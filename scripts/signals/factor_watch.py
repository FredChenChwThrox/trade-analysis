"""行业因子快照公共查询层（底稿 factor_snapshot 段；后续日报因子偏离提醒复用）。

职责：把 macro_factors 池级数据按「个股 → 因子暴露映射」过滤成单股快照。
映射人工维护在 config/industry_factors.yaml（symbol 覆盖 > 行业，同
watchlist.industry_code 口径）；direction/note 仅供 skill 研判参考，
本模块不做任何弹性推导（弹性属 skill 判断层，随卡片 factor_assumptions 存档）。

对齐口径：
- 时间（§2.1）：外盘因子（market='GLOBAL'）对 A 股 as_of 取 trade_date < as_of
  的最新读数（T-1——外盘当日收盘在 A 股收盘时点尚不存在，用 trade_date <= as_of
  等于引入未来数据）；内盘（CN）取 trade_date <= as_of。
- 单位：close 为来源原生单位定点 TEXT，本模块不换算；与卡片假设的比较也在
  原生单位上进行（假设快照随卡存 code/unit）。
- stale：最新读数距 as_of 超过 5 个 CN 交易日 → status='stale'
  （沿用 fx_rates 降级口径，§3.7）。
- change_20d/60d：按因子自身读数序列（截止对齐后日期）向前数 20/60 个读数；
  样本不足 → None（§2.5 不猜）。连续合约换月跳变会被吃进变动值，v1 不拼接，
  由 skill 侧判读时注意。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import yaml

from scripts.adapters.common import sha256_file
from scripts.pipeline.db import CONFIG_DIR

INDUSTRY_FACTORS_CONFIG = CONFIG_DIR / "industry_factors.yaml"
MACRO_FACTORS_CONFIG = CONFIG_DIR / "macro_factors.yaml"
STALE_TRADING_DAYS = 5      # 沿用 fx_rates 降级口径（§3.7）
CHANGE_WINDOWS = (20, 60)   # 近 20/60 个读数变动


class IndustryFactorsError(ValueError):
    """industry_factors.yaml 配置错误（缺字段或 code 不在因子清单中）。"""


# ---------------------------------------------------------------- 映射加载

# 模块级缓存：按 (path, macro_path, 双文件 mtime) 命中，文件未变不重复解析 YAML。
# 单股页每次请求都走 get_stock_overview，磁盘 IO 是可观开销（1.6）。
_INDUSTRY_CACHE: tuple[Path, tuple[float, float], dict, str] | None = None


def _load_industry_factors_from_disk(
        path: Path, macro_path: Path) -> tuple[dict, str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if doc.get("schema_version") != 1:
        raise IndustryFactorsError(f"{path.name}: schema_version 须为 1")
    known = {f.get("code") for f in
             (yaml.safe_load(macro_path.read_text(encoding="utf-8")) or {})
             .get("factors", [])}
    mapping = {"industries": {}, "symbols": {}}
    for scope in ("industries", "symbols"):
        for key, entries in (doc.get(scope) or {}).items():
            checked = []
            for e in entries or []:
                code = (e.get("code") or "").strip()
                if not code:
                    raise IndustryFactorsError(f"{path.name} {scope}.{key}: 缺 code")
                if code not in known:
                    raise IndustryFactorsError(
                        f"{path.name} {scope}.{key}: code {code} 不在"
                        f" macro_factors.yaml 因子清单中")
                checked.append({
                    "code": code,
                    "direction": (e.get("direction") or "").strip() or None,
                    "note": (e.get("note") or "").strip() or None,
                })
            mapping[scope][str(key)] = checked
    return mapping, sha256_file(path)


def load_industry_factors(
        path: Path | None = None,
        macro_path: Path | None = None) -> tuple[dict, str]:
    """读 industry_factors.yaml，返回 (mapping, 内容哈希)。

    mapping = {"industries": {bk: [entry, ...]}, "symbols": {sym: [entry, ...]}}，
    entry = {"code", "direction", "note"}。code 必须在 macro_factors.yaml
    因子清单中，否则抛 IndustryFactorsError（配置拼错立即报错，不静默跳过）。
    双文件 mtime 未变时返回模块级缓存（1.6：单股页请求避免重复解析 YAML）。
    """
    global _INDUSTRY_CACHE
    path = path or INDUSTRY_FACTORS_CONFIG
    macro_path = macro_path or MACRO_FACTORS_CONFIG
    mtime = (path.stat().st_mtime, macro_path.stat().st_mtime)
    if _INDUSTRY_CACHE and _INDUSTRY_CACHE[0] == path \
            and _INDUSTRY_CACHE[1] == mtime:
        return _INDUSTRY_CACHE[2], _INDUSTRY_CACHE[3]
    mapping, h = _load_industry_factors_from_disk(path, macro_path)
    _INDUSTRY_CACHE = (path, mtime, mapping, h)
    return mapping, h


def factors_for_symbol(mapping: dict, symbol: str,
                       industry_code: str | None) -> list[dict]:
    """解析个股因子清单：symbol 覆盖 > 行业映射；都无 → []。"""
    if symbol in mapping["symbols"]:
        return mapping["symbols"][symbol]
    if industry_code and industry_code in mapping["industries"]:
        return mapping["industries"][industry_code]
    return []


# ---------------------------------------------------------------- 因子读数

def latest_factor_close(conn: sqlite3.Connection, code: str, as_of: str,
                        market: str) -> dict | None:
    """对齐 as_of 的最新因子读数；无任何读数返回 None。

    GLOBAL 因子取 trade_date < as_of（T-1），CN 取 trade_date <= as_of。
    status：ok / stale（最新读数距 as_of 超过 5 个 CN 交易日）。
    操作符不进 f-string（SQLite 不支持把操作符参数化，按 market 拆两个查询）。
    """
    if market == "GLOBAL":
        row = conn.execute(
            """
            SELECT factor_type, code, name, market, trade_date, close, change_pct, unit
            FROM macro_factors
            WHERE code = ? AND trade_date < ?
            ORDER BY trade_date DESC LIMIT 1
            """, (code, as_of)).fetchone()
    else:
        row = conn.execute(
            """
            SELECT factor_type, code, name, market, trade_date, close, change_pct, unit
            FROM macro_factors
            WHERE code = ? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
            """, (code, as_of)).fetchone()
    if row is None:
        return None
    lag = conn.execute(
        """
        SELECT COUNT(*) FROM trading_calendar
        WHERE market = 'CN' AND is_open = 1 AND trade_date > ? AND trade_date <= ?
        """,
        (row["trade_date"], as_of)).fetchone()[0]
    return {
        "code": row["code"],
        "name": row["name"],
        "market": row["market"],
        "trade_date": row["trade_date"],
        "close": row["close"],
        "unit": row["unit"],
        "status": "stale" if lag > STALE_TRADING_DAYS else "ok",
    }


def _change(conn: sqlite3.Connection, code: str, cutoff_date: str,
            window: int) -> float | None:
    """相对 cutoff_date 读数向前第 window 个读数的变动率；样本不足 None。"""
    rows = conn.execute(
        """
        SELECT close FROM macro_factors
        WHERE code = ? AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT ?
        """,
        (code, cutoff_date, window + 1)).fetchall()
    if len(rows) < window + 1:
        return None
    cur, prev = Decimal(rows[0]["close"]), Decimal(rows[-1]["close"])
    if prev == 0:
        return None
    return round(float(cur / prev - 1), 6)


def _factor_markets(conn: sqlite3.Connection, codes: list[str]) -> dict:
    """批量取因子 market（单次 IN 查询，避免逐因子 N+1，1.5）。"""
    if not codes:
        return {}
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, MAX(market) AS market FROM macro_factors "
        f"WHERE code IN ({placeholders}) GROUP BY code",
        list(codes)).fetchall()
    return {r["code"]: r["market"] for r in rows}


def snapshot_for_symbol(conn: sqlite3.Connection, symbol: str, as_of: str,
                        industry_code: str | None, mapping: dict) -> dict:
    """底稿 factor_snapshot 段：{factors: [...], note, as_of}。"""
    entries = factors_for_symbol(mapping, symbol, industry_code)
    if not entries:
        return {"as_of": as_of, "factors": [],
                "note": "无因子映射（config/industry_factors.yaml 未覆盖该股"
                        "行业/个股），skill 侧禁止编造因子读数"}
    markets = _factor_markets(conn, [e["code"] for e in entries])
    factors = []
    for e in entries:
        latest = latest_factor_close(
            conn, e["code"], as_of, markets.get(e["code"], "CN"))
        if latest is None:
            factors.append({**e, "close": None, "trade_date": None,
                            "unit": None, "market": markets.get(e["code"], "CN"),
                            "status": "missing",
                            "change_20d": None, "change_60d": None})
            continue
        factors.append({
            **e,
            "name": latest["name"],
            "close": latest["close"],
            "trade_date": latest["trade_date"],
            "unit": latest["unit"],
            "market": latest["market"],
            "status": latest["status"],
            "change_20d": _change(conn, e["code"], latest["trade_date"], 20),
            "change_60d": _change(conn, e["code"], latest["trade_date"], 60),
        })
    return {"as_of": as_of, "factors": factors, "note": None}
