"""排期卡读取与机械换算（D2.2/D2.4 公共层，设计 §5.4、§5.4b、§5.6）。

生效区间语义（本模块锁定并供全信号层共用）：
- 卡片对交易日 d 生效当且仅当 `effective_from <= d < effective_to`
  （effective_to 为 NULL 表示开口区间）；status 为 active/superseded 的版本才可能
  有生效区间，draft/rejected 从未生效。
- 激活新版本时旧版本 `effective_to = 新版本 effective_from`（排他端点），
  同一交易日只有一张卡生效（§5.1：卡片相关信号只对卡片实际生效区间计算）。

卡片 JSON 字段约定（第一版 schema，D3.3 生成器与 skils 侧遵守同一约定；
价格类关键决策值一律十进制字符串，§9.5）：

- price_tiers_json:        {"tiers": [{"tier": 1, "zone_low": "55.00",
                            "zone_high": "58.00"}, ...]}（不复权绝对价位）
- invalidation_json:       {"line": "47.00", "note": ...}
- swing_box_json:          {"box_low", "box_high", "buy_zone_low", "buy_zone_high",
                            "sell_zone_low", "sell_zone_high", "box_invalidation"}
- right_side_trigger_json: {"trigger_level": "60.00", "stop_level": "56.00"}
- earnings_scenarios_json: {"eps": {"bear": "3.20", "base": "3.70", "bull": "4.20"}}
- valuation_scenarios_json:{"pe": {...}, ...}（换算不动，§5.4b）

机械换算（§5.4b 第二步，纯 Python 不经 LLM）：
- 送转/拆股：全部价格类字段 × 倍率的倒数（10 送 10 → ×0.5），EPS × 同一因子
  （每股口径同步变化），PE 情景/胜率/档位比例不动；
- 现金分红：全部价格类字段 − 每股分红额（除权价 = 原价 − D），EPS/PE 不动。
换算结果统一量化为 4 位小数（ROUND_HALF_UP），精确因子/金额记入
input_snapshot_json.conversion。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

Q4 = Decimal("0.0001")


def dec_str(d: Decimal) -> str:
    """定点输出（4 位小数，禁用科学计数法）。"""
    return format(d.quantize(Q4, rounding=ROUND_HALF_UP), "f")


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    return Decimal(str(v))


# ---------------------------------------------------------------- 读取

@dataclass
class Card:
    """一张已生效（或曾生效）的排期卡版本，JSON 字段已解析为 Decimal。"""

    card_version_id: str
    symbol: str
    status: str
    effective_from: str | None
    effective_to: str | None
    supersedes_id: str | None
    tiers: list[dict] = field(default_factory=list)   # tier/zone_low/zone_high(Decimal)
    invalidation_line: Decimal | None = None
    swing_box: dict = field(default_factory=dict)     # key -> Decimal
    trigger_level: Decimal | None = None
    stop_level: Decimal | None = None

    def covers(self, trade_date: str) -> bool:
        """生效区间 [effective_from, effective_to)（§5.1）。"""
        if self.effective_from is None or trade_date < self.effective_from:
            return False
        return self.effective_to is None or trade_date < self.effective_to


SWING_BOX_KEYS = ("box_low", "box_high", "buy_zone_low", "buy_zone_high",
                  "sell_zone_low", "sell_zone_high", "box_invalidation")


def parse_card(row: sqlite3.Row) -> Card:
    card = Card(
        card_version_id=row["card_version_id"],
        symbol=row["symbol"],
        status=row["status"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        supersedes_id=row["supersedes_id"],
    )
    if row["price_tiers_json"]:
        for t in (json.loads(row["price_tiers_json"]).get("tiers") or []):
            card.tiers.append({
                "tier": t.get("tier"),
                "zone_low": _dec(t.get("zone_low")),
                "zone_high": _dec(t.get("zone_high")),
            })
    if row["invalidation_json"]:
        card.invalidation_line = _dec(json.loads(row["invalidation_json"]).get("line"))
    if row["swing_box_json"]:
        box = json.loads(row["swing_box_json"])
        card.swing_box = {k: _dec(box.get(k)) for k in SWING_BOX_KEYS if box.get(k) is not None}
    if row["right_side_trigger_json"]:
        rst = json.loads(row["right_side_trigger_json"])
        card.trigger_level = _dec(rst.get("trigger_level"))
        card.stop_level = _dec(rst.get("stop_level"))
    return card


def load_card_versions(conn: sqlite3.Connection, symbol: str) -> list[Card]:
    """该股全部曾/正生效版本（active + superseded），按 effective_from 排序。"""
    rows = conn.execute(
        """
        SELECT * FROM strategy_card_versions
        WHERE symbol = ? AND status IN ('active', 'superseded')
        ORDER BY effective_from
        """,
        (symbol,),
    ).fetchall()
    return [parse_card(r) for r in rows]


def card_for_day(versions: list[Card], trade_date: str) -> Card | None:
    """该交易日生效的版本（同一交易日至多一张，§5.1）。"""
    for c in versions:
        if c.covers(trade_date):
            return c
    return None


def load_active_card(conn: sqlite3.Connection, symbol: str,
                     trade_date: str) -> Card | None:
    """trade_date 当日生效的 active 版本（无则 None——调用方按 §2.5 记 incomplete）。"""
    row = conn.execute(
        """
        SELECT * FROM strategy_card_versions
        WHERE symbol = ? AND status = 'active'
          AND effective_from IS NOT NULL AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to > ?)
        """,
        (symbol, trade_date, trade_date),
    ).fetchone()
    return parse_card(row) if row else None


# ---------------------------------------------------------------- 机械换算（§5.4b 第二步）

def convert_card_fields(row: sqlite3.Row, op: str, amount: Decimal) -> dict[str, str]:
    """机械换算卡片价格类字段。op='multiply'（× 1/倍率）或 'subtract'（− 每股分红）。

    返回 {列名: 新 JSON 文本}；只改价格类字段（multiply 另改 EPS），
    valuation_scenarios_json（PE 刻度）原样保留（§5.4b：PE 情景不变）。
    """
    fn = (lambda x: x * amount) if op == "multiply" else (lambda x: x - amount)
    out: dict[str, str] = {}

    if row["price_tiers_json"]:
        doc = json.loads(row["price_tiers_json"])
        for t in doc.get("tiers") or []:
            for k in ("zone_low", "zone_high"):
                if t.get(k) is not None:
                    t[k] = dec_str(fn(Decimal(str(t[k]))))
        out["price_tiers_json"] = json.dumps(doc, ensure_ascii=False, sort_keys=True)

    if row["invalidation_json"]:
        doc = json.loads(row["invalidation_json"])
        if doc.get("line") is not None:
            doc["line"] = dec_str(fn(Decimal(str(doc["line"]))))
        out["invalidation_json"] = json.dumps(doc, ensure_ascii=False, sort_keys=True)

    if row["swing_box_json"]:
        doc = json.loads(row["swing_box_json"])
        for k in SWING_BOX_KEYS:
            if doc.get(k) is not None:
                doc[k] = dec_str(fn(Decimal(str(doc[k]))))
        out["swing_box_json"] = json.dumps(doc, ensure_ascii=False, sort_keys=True)

    if row["right_side_trigger_json"]:
        doc = json.loads(row["right_side_trigger_json"])
        for k in ("trigger_level", "stop_level"):
            if doc.get(k) is not None:
                doc[k] = dec_str(fn(Decimal(str(doc[k]))))
        out["right_side_trigger_json"] = json.dumps(doc, ensure_ascii=False, sort_keys=True)

    if op == "multiply" and row["earnings_scenarios_json"]:
        # EPS 每股口径同步 ÷ 倍率（= × amount）；现金分红不改 EPS（§5.4b）
        doc = json.loads(row["earnings_scenarios_json"])
        eps = doc.get("eps") or {}
        for k, v in eps.items():
            if v is not None:
                eps[k] = dec_str(fn(Decimal(str(v))))
        out["earnings_scenarios_json"] = json.dumps(doc, ensure_ascii=False, sort_keys=True)

    return out


def conversion_snapshot(source_card_id: str, ca: sqlite3.Row, op: str,
                        amount: Decimal, extra: dict | None = None) -> dict:
    """input_snapshot_json.conversion 标准结构（换算来源版本/事件/倍率或金额）。"""
    snap = {
        "conversion": {
            "ca_id": ca["ca_id"],
            "source_card_version_id": source_card_id,
            "ex_date": ca["ex_date"],
            "action_type": ca["action_type"],
            "op": op,
            "factor": dec_str(amount) if op == "multiply" else None,
            "cash_per_share": str(ca["cash_per_share"]) if op == "subtract" else None,
        }
    }
    if extra:
        snap["conversion"].update(extra)
    return snap


def handled_ca_ids(conn: sqlite3.Connection, symbol: str) -> set[int]:
    """已换算处理过的公司行为 ca_id（任一版本 input_snapshot_json 记录过）。"""
    ids: set[int] = set()
    rows = conn.execute(
        "SELECT input_snapshot_json FROM strategy_card_versions WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    for r in rows:
        if not r["input_snapshot_json"]:
            continue
        conv = (json.loads(r["input_snapshot_json"]) or {}).get("conversion") or {}
        if conv.get("ca_id") is not None:
            ids.add(int(conv["ca_id"]))
    return ids
