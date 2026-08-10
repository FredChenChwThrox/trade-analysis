"""排期卡生成底稿导出器（D3.3 第一步，设计 §5.6、§3.2、§3.4）。

把估值排期卡所需的全部事实从 data/market.db 导出为一份底稿 JSON
（`cards/{symbol}/inputs_{YYYY-MM-DD}.json`，日期 = 最新交易日），供
fred-valuation-card-skill 作为主输入消费（Token 节约：LLM 只消费存档事实，
缺口数据才回源 kimi-datasource 插件）。

底稿九段（schema card_inputs_v1）：
1. meta              标的信息 + 各来源数据截止日期 + 口径注记；
2. earnings          盈利底稿：年报+季报营收/归母净利/EPS 及同比序列、TTM 现值；
3. forecasts         最新一致预期 FY1–FY3 与最近季报实际增速的裂口对照；
4. valuation_scale   估值刻度候选：恐慌低点清单（复权/不复权价 + 当日 pe_ttm）、
                     pe_ttm 分位数（5/25/50/75/95）与当前值；**样本区间强制标注（§3.2）**；
5. market_snapshot   现价（不复权）、当前 PE(TTM)、总股本；
6. exhaustion_params 衰竭信号具体化参数：当前锚点不复权前低、下跌起点后前 4 周
                     均量基数、2 倍放量阈值、40–60% 缩量阈值（config/signals.yaml 实数）；
7. signal_status     当前完成周五项衰竭信号状态与活跃计数（≥2 项口径）；
8. daily_watch       当前档位监测（tier/证伪/箱体/均线）最近 facts 摘要 + active 卡概要；
9. config_params     参与计算的信号参数回声与 config_hash（§4.2 可追溯）。

纯读取，不写库。价格口径：价区/前低一律不复权（§3.4），量能为调整量
（与周线信号口径一致）。

CLI：
    uv run python -m scripts.pipeline.card_inputs <symbol> [--db PATH] [--out-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from scripts.indicators import valuation
from scripts.pipeline.db import DEFAULT_DB_PATH, ROOT, connect, utc_now
from scripts.signals import cards as card_mod
from scripts.signals.common import RULE_VERSION, WEEKLY_SIGNALS, load_params
from scripts.signals.daily_watch import DAILY_WATCH_SIGNALS
from scripts.signals.exhaustion import count_active_signals

SCHEMA = "card_inputs_v1"
QUANTILE_PS = (5, 25, 50, 75, 95)


class CardInputsError(Exception):
    """可预期的导出错误（退出码 2）。"""


# ---------------------------------------------------------------- 工具

def percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数（numpy linear 口径）；空序列返回 None。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    rank = p / 100.0 * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


def _r4(v: float | None) -> float | None:
    return round(v, 4) if v is not None else None


def _r6(v: float | None) -> float | None:
    return round(v, 6) if v is not None else None


def _dec(v: str | None) -> Decimal | None:
    return Decimal(v) if v is not None else None


def _yoy(cur: Decimal | None, prev: Decimal | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return _r6(float(cur / prev - 1))


# ---------------------------------------------------------------- 各段构建

def _meta(conn: sqlite3.Connection, symbol: str) -> tuple[dict, sqlite3.Row]:
    wl = conn.execute(
        "SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    if wl is None:
        raise CardInputsError(f"{symbol} 不在 watchlist")
    cutoff = {}
    for table, col in (("daily_bars", "trade_date"),
                       ("indicators_daily", "trade_date"),
                       ("weekly_bars", "week_end_date")):
        cutoff[table] = conn.execute(
            f"SELECT MAX({col}) FROM {table} WHERE symbol = ?", (symbol,),
        ).fetchone()[0]
    cutoff["financial_reports"] = conn.execute(
        "SELECT MAX(period_end) FROM financial_reports WHERE symbol = ?",
        (symbol,)).fetchone()[0]
    cutoff["forecasts_snapshot"] = conn.execute(
        "SELECT MAX(snapshot_at) FROM forecasts WHERE symbol = ?",
        (symbol,)).fetchone()[0]
    if cutoff["daily_bars"] is None:
        raise CardInputsError(f"{symbol} 无 daily_bars，先入库行情")
    meta = {
        "schema": SCHEMA,
        "symbol": symbol,
        "name": wl["name"],
        "market": wl["market"],
        "currency": wl["currency"],
        "timezone": wl["timezone"],
        "generated_at": utc_now(),
        "data_cutoff": cutoff,
        "notes": [
            "价格口径：价区/前低/现价一律不复权（raw），复权价仅供技术比较（§3.4）",
            "PE(TTM) 与 TTM 为市值口径（不复权价×股本÷TTM 归母净利），"
            "财报 available_at 为入库降级口径（与 pe_status degraded_available_at 一致）",
            "量能阈值为调整量（volume_adj）口径，与周线衰竭信号一致",
        ],
    }
    return meta, wl


def _earnings(conn: sqlite3.Connection, symbol: str,
              shares: Decimal | None) -> dict:
    rows = conn.execute(
        """
        SELECT r.period_end, r.period_type, r.fiscal_year, r.is_cumulative,
               r.currency, f.revenue, f.net_profit_attr, f.eps_basic
        FROM financial_reports r
        LEFT JOIN financial_facts f ON f.report_id = r.report_id
        WHERE r.symbol = ? ORDER BY r.period_end
        """,
        (symbol,),
    ).fetchall()
    by_key = {}
    for r in rows:
        by_key[(r["period_type"], bool(r["is_cumulative"]),
                r["period_end"][5:], r["fiscal_year"])] = r

    reports = []
    for r in rows:
        prev = by_key.get((r["period_type"], bool(r["is_cumulative"]),
                           r["period_end"][5:], r["fiscal_year"] - 1))
        reports.append({
            "period_end": r["period_end"],
            "period_type": r["period_type"],
            "fiscal_year": r["fiscal_year"],
            "is_cumulative": bool(r["is_cumulative"]),
            "revenue": r["revenue"],
            "net_profit_attr": r["net_profit_attr"],
            "eps_basic": r["eps_basic"],
            "revenue_yoy": _yoy(_dec(r["revenue"]),
                                _dec(prev["revenue"]) if prev else None),
            "net_profit_yoy": _yoy(_dec(r["net_profit_attr"]),
                                   _dec(prev["net_profit_attr"]) if prev else None),
        })

    # TTM：全量报告（assume_visible，与 indicators_daily 降级口径一致）
    ttm, reason = valuation.ttm_net_profit(valuation.load_reports(conn, symbol))
    ttm_eps = float(ttm / shares) if (ttm is not None and shares) else None
    latest = reports[-1] if reports else None
    return {
        "reports": reports,
        "ttm": {
            "net_profit_attr": str(ttm) if ttm is not None else None,
            "eps": _r4(ttm_eps),
            "status": reason,
            "shares": str(shares) if shares else None,
            "note": "TTM = 最新年报 + 本财年最新累计 − 上一财年同期累计（§3.7）；"
                    "EPS = TTM ÷ 最新股本",
        },
        "latest_report": latest,
        "currency": rows[-1]["currency"] if rows else None,
    }


def _forecasts(conn: sqlite3.Connection, symbol: str,
               earnings: dict) -> dict:
    row = conn.execute(
        "SELECT snapshot_at, source, payload_json FROM forecasts "
        "WHERE symbol = ? ORDER BY snapshot_at DESC LIMIT 1",
        (symbol,)).fetchone()
    if row is None:
        return {"snapshot_at": None, "note": "无一致预期快照（缺口数据，"
                "skill 侧可回源 stock_finance_data_get_forecast）"}
    payload = json.loads(row["payload_json"])
    rec = next((r for r in payload.get("rows", [])
                if (r.get("ths_fore_np_fy1_stock") or "").strip()), None)
    out = {"snapshot_at": row["snapshot_at"], "source": row["source"]}
    if rec is None:
        out["note"] = "最新快照无 FY1 净利预测行"
        return out

    def num(key: str) -> Decimal | None:
        v = (rec.get(key) or "").strip()
        return Decimal(v) if v else None

    fy1, fy2, fy3 = (num("ths_fore_np_fy1_stock"), num("ths_fore_np_fy2_stock"),
                     num("ths_fore_np_fy3_stock"))
    fy1_yoy_raw = num("ths_fore_np_yoy_stock")  # 来源为百分数，归一为分数
    yoy = {"fy1": _r6(float(fy1_yoy_raw / 100)) if fy1_yoy_raw is not None else None,
           "fy2": _yoy(fy2, fy1), "fy3": _yoy(fy3, fy2)}
    fy1_year = int(row["snapshot_at"][:4])  # 快照年 = FY1（约定锁定）
    out.update({
        "fy1_year": fy1_year,
        "net_profit": {"fy1": str(fy1) if fy1 else None,
                       "fy2": str(fy2) if fy2 else None,
                       "fy3": str(fy3) if fy3 else None},
        "revenue": {"fy1": (rec.get("ths_fore_mbi_fy1_stock") or None),
                    "fy2": (rec.get("ths_fore_mbi_fy2_stock") or None),
                    "fy3": (rec.get("ths_fore_mbi_fy3_stock") or None)},
        "yoy_pct": yoy,
    })
    latest = earnings.get("latest_report")
    if latest and yoy["fy1"] is not None and latest.get("net_profit_yoy") is not None:
        out["gap_check"] = {
            "actual_period": latest["period_end"],
            "actual_net_profit_yoy": latest["net_profit_yoy"],
            "forecast_fy1_yoy": yoy["fy1"],
            "gap_pp": _r4((yoy["fy1"] - latest["net_profit_yoy"]) * 100),
            "note": "裂口 = 券商 FY1 增速预期 − 最近季报实际增速（百分点）；"
                    "裂口显著时情景设定以实际趋势为准（skill 第 2 步纪律）",
        }
    return out


def _valuation_scale(conn: sqlite3.Connection, symbol: str) -> dict:
    pe_rows = conn.execute(
        "SELECT trade_date, pe_ttm FROM indicators_daily "
        "WHERE symbol = ? AND pe_ttm IS NOT NULL ORDER BY trade_date",
        (symbol,)).fetchall()
    if not pe_rows:
        raise CardInputsError(f"{symbol} indicators_daily 无 pe_ttm，先重算指标")
    pe_by_date = {r["trade_date"]: r["pe_ttm"] for r in pe_rows}
    series = [r["pe_ttm"] for r in pe_rows]

    # 恐慌低点清单：去重锚点身份（trade_date 唯一），首次出现 as_of 一并记录
    lows = conn.execute(
        """
        SELECT trade_date, adjusted_price, raw_price, is_fallback, MIN(as_of) AS first_as_of
        FROM weekly_anchors WHERE symbol = ? AND anchor_type = 'panic_low'
        GROUP BY trade_date, adjusted_price, raw_price, is_fallback
        ORDER BY trade_date
        """,
        (symbol,)).fetchall()
    panic_lows = [{
        "trade_date": r["trade_date"],
        "raw_price": r["raw_price"],
        "adjusted_price": _r4(r["adjusted_price"]),
        "is_fallback": bool(r["is_fallback"]),
        "pe_ttm": _r4(pe_by_date.get(r["trade_date"])),
        "first_as_of": r["first_as_of"],
    } for r in lows]

    quantiles = {f"p{p}": _r4(percentile(series, p)) for p in QUANTILE_PS}
    return {
        "panic_lows": panic_lows,
        "pe_ttm_quantiles": quantiles,
        "current_pe_ttm": _r4(series[-1]),
        "sample_window": {
            "start": pe_rows[0]["trade_date"],
            "end": pe_rows[-1]["trade_date"],
            "n_days": len(series),
            "n_panic_lows": len(panic_lows),
            "note": "PE 刻度样本区间强制标注（§3.2）：第一版仅 3 年样本，"
                    "引用更早历史估值须声明为样本外，禁止当作同体系刻度",
        },
    }


def _market_snapshot(conn: sqlite3.Connection, symbol: str,
                     shares: Decimal | None) -> dict:
    bar = conn.execute(
        "SELECT trade_date, close_raw FROM daily_bars WHERE symbol = ? "
        "ORDER BY trade_date DESC LIMIT 1", (symbol,)).fetchone()
    pe = conn.execute(
        "SELECT pe_ttm, pe_status FROM indicators_daily WHERE symbol = ? "
        "ORDER BY trade_date DESC LIMIT 1", (symbol,)).fetchone()
    return {
        "trade_date": bar["trade_date"],
        "close_raw": bar["close_raw"],
        "pe_ttm": _r4(pe["pe_ttm"]) if pe else None,
        "pe_status": pe["pe_status"] if pe else None,
        "shares_issued": str(shares) if shares else None,
        "price_basis": "raw（不复权绝对价位）",
    }


def _exhaustion_params(conn: sqlite3.Connection, symbol: str,
                       params: dict) -> dict:
    latest_asof = conn.execute(
        "SELECT MAX(as_of) FROM weekly_anchors WHERE symbol = ?", (symbol,),
    ).fetchone()[0]
    if latest_asof is None:
        return {"note": "无 weekly_anchors，先运行 weekly_signals"}
    anchors = {}
    for r in conn.execute(
            "SELECT anchor_type, trade_date, adjusted_price, raw_price, is_fallback "
            "FROM weekly_anchors WHERE symbol = ? AND as_of = ?", (symbol, latest_asof)):
        anchors[r["anchor_type"]] = {
            "trade_date": r["trade_date"],
            "raw_price": r["raw_price"],
            "adjusted_price": _r4(r["adjusted_price"]),
            "is_fallback": bool(r["is_fallback"]),
        }
    ex = params["exhaustion"]
    d = ex["dryup"]
    out = {
        "as_of": latest_asof,
        "anchor": anchors,
        "prev_low_raw": (anchors.get("panic_low") or {}).get("raw_price"),
        "params_echo": {
            "vol_multiple": ex["panic"]["vol_multiple"],
            "dryup_base_weeks": d["base_weeks"],
            "dryup_vol_ratio": d["vol_ratio"],
            "dryup_vol_ratio_band": [0.40, 0.60],
            "no_new_low_weeks": ex["no_new_low_weeks"],
            "duration_weeks": ex["duration_weeks"],
            "min_active_signals": ex["min_active_signals"],
        },
    }
    decline = anchors.get("decline_start")
    if decline is None:
        out["note"] = "无下跌起点锚点，干涸基数不可得"
        return out
    weeks = conn.execute(
        "SELECT week_end_date, volume_adj FROM weekly_bars WHERE symbol = ? "
        "ORDER BY week_end_date", (symbol,)).fetchall()
    idx = next((i for i, w in enumerate(weeks)
                if w["week_end_date"] == decline["trade_date"]), None)
    if idx is None:
        out["note"] = "下跌起点周不在 weekly_bars，干涸基数不可得"
        return out
    bw = d["base_weeks"]
    base_weeks = weeks[idx + 1: idx + 1 + bw]
    if len(base_weeks) < bw:
        out["note"] = f"下跌起点后完成周不足 {bw} 周，干涸基数不可得"
        return out
    vols = [w["volume_adj"] for w in base_weeks]
    mean = sum(vols) / bw
    out["base"] = {
        "decline_week": decline["trade_date"],
        "base_weeks": [w["week_end_date"] for w in base_weeks],
        "volumes_adj": [_r4(v) for v in vols],
        "mean_volume_adj": _r4(mean),
        "note": "下跌起点后前 4 个完成周均量（调整量，不含当前周，§4.1 shift(1)）",
    }
    out["thresholds"] = {
        "panic_volume_x2": _r4(mean * ex["panic"]["vol_multiple"]),
        "dryup_volume_040": _r4(mean * 0.40),
        "dryup_volume_050": _r4(mean * d["vol_ratio"]),
        "dryup_volume_060": _r4(mean * 0.60),
    }
    return out


def _signal_status(conn: sqlite3.Connection, symbol: str,
                   params: dict) -> dict:
    last_week = conn.execute(
        "SELECT MAX(week_end_date) FROM weekly_bars WHERE symbol = ?",
        (symbol,)).fetchone()[0]
    if last_week is None:
        return {"note": "无 weekly_bars"}
    min_active = params["exhaustion"]["min_active_signals"]
    active = count_active_signals(conn, symbol, last_week, min_active)
    signals = []
    for r in conn.execute(
            f"SELECT signal, state, triggered, active_until, details_json "
            f"FROM signal_facts WHERE symbol = ? AND observed_on = ? AND signal IN "
            f"({', '.join('?' * len(WEEKLY_SIGNALS))}) ORDER BY signal",
            (symbol, last_week, *WEEKLY_SIGNALS)):
        det = json.loads(r["details_json"]) if r["details_json"] else {}
        signals.append({
            "signal": r["signal"], "state": r["state"],
            "triggered": bool(r["triggered"]), "active_until": r["active_until"],
            "reason": det.get("reason"),
        })
    return {
        "week_end_date": last_week,
        "anchor_id": active["anchor_id"],
        "active_count": active["active_count"],
        "active_signals": active["active_signals"],
        "min_active_signals": min_active,
        "meets_min": active["meets_min"],
        "signals": signals,
    }


def _daily_watch(conn: sqlite3.Connection, symbol: str) -> dict:
    as_of = conn.execute(
        "SELECT MAX(trade_date) FROM daily_bars WHERE symbol = ?", (symbol,),
    ).fetchone()[0]
    facts = {}
    for sig in DAILY_WATCH_SIGNALS:
        r = conn.execute(
            "SELECT observed_on, state, triggered FROM signal_facts "
            "WHERE symbol = ? AND signal = ? ORDER BY observed_on DESC LIMIT 1",
            (symbol, sig)).fetchone()
        if r:
            facts[sig] = {"observed_on": r["observed_on"], "state": r["state"],
                          "triggered": bool(r["triggered"])}
    card = card_mod.load_active_card(conn, symbol, as_of)
    card_brief = None
    if card is not None:
        card_brief = {
            "card_version_id": card.card_version_id,
            "effective_from": card.effective_from,
            "tiers": [{"tier": t["tier"],
                       "zone_low": str(t["zone_low"]),
                       "zone_high": str(t["zone_high"])} for t in card.tiers],
            "invalidation_line": (str(card.invalidation_line)
                                  if card.invalidation_line is not None else None),
        }
    return {
        "as_of": as_of,
        "active_card": card_brief,
        "facts": facts,
        "note": None if card else "当前无 active 卡（§2.5：卡片相关监测为 incomplete）",
    }


def _config_params(params: dict, config_hash: str) -> dict:
    return {
        "config_hash": config_hash,
        "rule_version": RULE_VERSION,
        "source": "config/signals.yaml defaults",
        "anchors": params["anchors"],
        "exhaustion": {
            "panic": params["exhaustion"]["panic"],
            "dryup": params["exhaustion"]["dryup"],
            "no_new_low_weeks": params["exhaustion"]["no_new_low_weeks"],
            "duration_weeks": params["exhaustion"]["duration_weeks"],
            "active_weeks": params["exhaustion"]["active_weeks"],
            "min_active_signals": params["exhaustion"]["min_active_signals"],
        },
        "daily_watch": params["daily_watch"],
        "right_side": params["right_side"],
    }


# ---------------------------------------------------------------- 顶层

def build_inputs(conn: sqlite3.Connection, symbol: str,
                 params: dict | None = None,
                 config_hash: str | None = None) -> dict:
    """构建九段底稿 dict（纯读取）。"""
    if params is None or config_hash is None:
        params, config_hash = load_params()
    meta, _ = _meta(conn, symbol)
    share_events = valuation.load_share_events(conn, symbol)
    shares = valuation.shares_at(share_events, meta["data_cutoff"]["daily_bars"])
    earnings = _earnings(conn, symbol, shares)
    return {
        "meta": meta,
        "earnings": earnings,
        "forecasts": _forecasts(conn, symbol, earnings),
        "valuation_scale": _valuation_scale(conn, symbol),
        "market_snapshot": _market_snapshot(conn, symbol, shares),
        "exhaustion_params": _exhaustion_params(conn, symbol, params),
        "signal_status": _signal_status(conn, symbol, params),
        "daily_watch": _daily_watch(conn, symbol),
        "config_params": _config_params(params, config_hash),
    }


def export_inputs(conn: sqlite3.Connection, symbol: str,
                  out_dir: Path | None = None) -> tuple[dict, Path]:
    """构建底稿并写 `cards/{symbol}/inputs_{最新交易日}.json`。"""
    doc = build_inputs(conn, symbol)
    out_dir = out_dir or (ROOT / "cards" / symbol)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"inputs_{doc['meta']['data_cutoff']['daily_bars']}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return doc, path


def _summary(doc: dict, path: Path) -> str:
    e, v, m = doc["earnings"], doc["valuation_scale"], doc["market_snapshot"]
    s, x = doc["signal_status"], doc["exhaustion_params"]
    ttm = _dec(e["ttm"]["net_profit_attr"])
    ttm_yi = f"{float(ttm) / 1e8:.2f} 亿" if ttm is not None else "—"
    lines = [
        f"{doc['meta']['symbol']}（{doc['meta']['name']}）底稿 → {path}",
        f"数据截止: 行情 {m['trade_date']} / 财报 {e['latest_report']['period_end'] if e['latest_report'] else '—'}"
        f" / 一致预期 {doc['forecasts'].get('snapshot_at') or '—'}",
        f"现价 {m['close_raw']}（不复权） PE(TTM) {m['pe_ttm']} TTM 归母净利 {ttm_yi}"
        f" TTM EPS {e['ttm']['eps']}",
        f"PE 分位 p5/p50/p95 = {v['pe_ttm_quantiles']['p5']}"
        f" / {v['pe_ttm_quantiles']['p50']} / {v['pe_ttm_quantiles']['p95']}"
        f"（样本 {v['sample_window']['start']} ~ {v['sample_window']['end']}，"
        f"恐慌低点 {v['sample_window']['n_panic_lows']} 个）",
        f"衰竭阈值（调整量）: 放量≥{x.get('thresholds', {}).get('panic_volume_x2')}"
        f" 缩量≤{x.get('thresholds', {}).get('dryup_volume_040')}"
        f"~{x.get('thresholds', {}).get('dryup_volume_060')} 前低(不复权) {x.get('prev_low_raw')}",
        f"当前完成周 {s.get('week_end_date')}：活跃信号 {s.get('active_count')} 项"
        f"（min {s.get('min_active_signals')}） {s.get('active_signals')}",
    ]
    gap = doc["forecasts"].get("gap_check")
    if gap:
        lines.append(f"一致预期裂口: FY1 预期 {gap['forecast_fy1_yoy']:.2%}"
                     f" vs {gap['actual_period']} 实际 {gap['actual_net_profit_yoy']:.2%}"
                     f"（{gap['gap_pp']}pp）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.card_inputs")
    parser.add_argument("symbol")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--out-dir", default=None,
                        help="输出目录（默认 cards/{symbol}/）")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        doc, path = export_inputs(conn, args.symbol,
                                  Path(args.out_dir) if args.out_dir else None)
        print(_summary(doc, path))
        return 0
    except CardInputsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
