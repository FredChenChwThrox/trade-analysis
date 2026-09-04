"""基本面深度分析底稿导出器（2026-09-03 新增，配套 skills/fundamental-analysis-skill）。

把个股基本面分析所需的全部事实从 data/market.db 导出为一份底稿 JSON
（`reports/{symbol}/fundamental_inputs_{最新交易日}.json`），供
fundamental-analysis-skill 作为主输入消费。纪律与 card_inputs 一致：
**LLM 只消费存档事实，规范化数字一律 Python 计算**；缺数据段标 incomplete 不猜（§2.5）。

底稿八段（schema fundamental_inputs_v1）：
1. meta                标的信息 + 各来源数据截止（含 BS/CF/摘要覆盖范围）+ 口径注记；
2. financials_multi_year  全期次利润表 + 资产负债表 + 现金流量表 + Python 派生指标
                       （净利率/ROE/资产负债率/有息负债/FCF/OCF 净利比；毛利率取自
                       THS 摘要快照并标注来源）；每期带 incomplete 清单；
3. forecasts           最新一致预期 FY1–FY3 + 裂口对照（复用 card_inputs）；
4. valuation           pe_ttm 分位/恐慌低点（复用 card_inputs）+ market 现价快照
                       + **隐含回报区间表**（一致预期 EPS × PE 历史分位 → 隐含价区，
                       替代 LLM 做 DCF）；
5. pool_comps          池内横截面对比（最新 PE/净利同比/ROE/资产负债率）；
6. events_summary      该股最近有效事件标签 + 未评价公告计数 + 近窗日历事项；
7. factor_snapshot     行业因子快照（复用 factor_watch）；
8. gaps                全部 incomplete 项汇总（skill 必须如实呈现）。

纯读取，不写库。价格口径：一律不复权（§3.4）。

CLI：
    uv run python -m scripts.pipeline.fundamental_inputs <symbol> [--db PATH] [--out-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from scripts.indicators import valuation
from scripts.pipeline import card_inputs
from scripts.pipeline.db import DEFAULT_DB_PATH, ROOT, connect
from scripts.signals import event_link, factor_watch

SCHEMA = "fundamental_inputs_v1"

_dec = card_inputs._dec
_r4 = card_inputs._r4
_r6 = card_inputs._r6
_yoy = card_inputs._yoy


class FundamentalInputsError(Exception):
    """可预期的导出错误（退出码 2）。"""


# ---------------------------------------------------------------- 工具

def _div(a: Decimal | None, b: Decimal | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return _r6(float(a / b))


def _sub(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None:
        return None
    return a - b


# ---------------------------------------------------------------- 段 2：多年财务 + 派生指标

def _latest_report_rows(conn: sqlite3.Connection, symbol: str) -> list[sqlite3.Row]:
    """每（period_end, period_type, is_cumulative）取最新 revision，联三大 facts。"""
    return conn.execute(
        """
        SELECT r.period_end, r.period_type, r.fiscal_year, r.published_at,
               f.revenue, f.net_profit_attr, f.eps_basic,
               b.total_assets, b.total_liabilities, b.total_equity_attr,
               b.monetary_fund, b.short_term_borrowing, b.long_term_borrowing,
               b.bonds_payable, b.noncurrent_liab_1y, b.inventory,
               b.accounts_receivable, b.goodwill,
               c.ocf, c.capex, c.icf, c.financing_cf
        FROM financial_reports r
        LEFT JOIN financial_facts f ON f.report_id = r.report_id
        LEFT JOIN balance_sheet_facts b ON b.report_id = r.report_id
        LEFT JOIN cash_flow_facts c ON c.report_id = r.report_id
        WHERE r.symbol = ? AND r.is_cumulative = 1
          AND r.report_id IN (
              SELECT MAX(report_id) FROM financial_reports
              WHERE symbol = ? AND is_cumulative = 1
              GROUP BY period_end, period_type)
        ORDER BY r.period_end
        """,
        (symbol, symbol),
    ).fetchall()


def _ths_indicator_map(conn: sqlite3.Connection, symbol: str) -> dict[str, dict]:
    """THS 摘要快照 → {period_end: {指标: 值}}（组冲突时 常用指标 优先）。"""
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT period_end, payload_json FROM financial_indicator_snapshots "
            "WHERE symbol = ? AND source = 'akshare'", (symbol,)):
        payload = json.loads(r["payload_json"])
        flat: dict[str, str] = {}
        for group, indicators in payload.items():
            for k, v in indicators.items():
                flat.setdefault(k, v)  # 先到的组优先（payload 组序 = 采集序，常用指标在前）
        # 常用指标组显式优先（防御 payload 组序变化）
        for k, v in (payload.get("常用指标") or {}).items():
            flat[k] = v
        out[r["period_end"]] = flat
    return out


def _financials_multi_year(conn: sqlite3.Connection, symbol: str) -> dict:
    rows = _latest_report_rows(conn, symbol)
    ths = _ths_indicator_map(conn, symbol)
    by_key = {(r["period_type"], r["period_end"][5:], r["fiscal_year"]): r
              for r in rows}
    periods = []
    n_bs_missing = n_cf_missing = 0
    for r in rows:
        prev = by_key.get((r["period_type"], r["period_end"][5:],
                           r["fiscal_year"] - 1))
        revenue, np_attr = _dec(r["revenue"]), _dec(r["net_profit_attr"])
        assets = _dec(r["total_assets"])
        liab = _dec(r["total_liabilities"])
        equity = _dec(r["total_equity_attr"])
        ocf, capex = _dec(r["ocf"]), _dec(r["capex"])
        debt_parts = [_dec(r["short_term_borrowing"]), _dec(r["long_term_borrowing"]),
                      _dec(r["bonds_payable"]), _dec(r["noncurrent_liab_1y"])]
        debt = (sum(p for p in debt_parts if p is not None)
                if any(p is not None for p in debt_parts) else None)
        gm_ths = (ths.get(r["period_end"]) or {}).get("毛利率")
        roe_ths = (ths.get(r["period_end"]) or {}).get("净资产收益率(ROE)")

        incomplete = []
        if r["total_assets"] is None:
            n_bs_missing += 1
            incomplete.append("资产负债表缺失")
        if r["ocf"] is None:
            n_cf_missing += 1
            incomplete.append("现金流量表缺失")
        if gm_ths is None:
            incomplete.append("毛利率：THS 摘要缺（营业成本未采集，Python 不可算）")

        periods.append({
            "period_end": r["period_end"],
            "period_type": r["period_type"],
            "fiscal_year": r["fiscal_year"],
            "published_at": r["published_at"],
            "revenue": r["revenue"],
            "net_profit_attr": r["net_profit_attr"],
            "eps_basic": r["eps_basic"],
            "revenue_yoy": _yoy(revenue, _dec(prev["revenue"]) if prev else None),
            "net_profit_yoy": _yoy(np_attr,
                                   _dec(prev["net_profit_attr"]) if prev else None),
            "balance_sheet": {
                "total_assets": r["total_assets"],
                "total_liabilities": r["total_liabilities"],
                "total_equity_attr": r["total_equity_attr"],
                "monetary_fund": r["monetary_fund"],
                "inventory": r["inventory"],
                "accounts_receivable": r["accounts_receivable"],
                "goodwill": r["goodwill"],
            },
            "cash_flow": {"ocf": r["ocf"], "capex": r["capex"], "icf": r["icf"],
                          "financing_cf": r["financing_cf"]},
            "derived": {
                "net_margin": _div(np_attr, revenue),
                "gross_margin_ths": gm_ths,
                "roe": _div(np_attr, equity),
                "roe_ths": roe_ths,
                "asset_liability_ratio": _div(liab, assets),
                "interest_bearing_debt": str(debt) if debt is not None else None,
                "fcf": str(_sub(ocf, capex)) if _sub(ocf, capex) is not None else None,
                "ocf_to_np": _div(ocf, np_attr),
            },
            "incomplete": incomplete,
        })
    return {
        "periods": periods,
        "notes": [
            "金额为累计口径（is_cumulative=1），同比=同年同 MM-DD 期次对比",
            "ROE = 归母净利(累计) ÷ 期末归母权益（非加权口径；中期为年内累计，"
            "跨年比较请用年报期次）；roe_ths 为 THS 摘要源算值，仅作交叉核对",
            "有息负债 = 短期借款+长期借款+应付债券+一年内到期非流动负债"
            "（缺失字段按 0 处理，四项全缺则为 null）",
            "FCF = 经营现金流净额 − 购建固定资产等支付现金；OCF/净利比 = OCF ÷ 归母净利",
            "毛利率取自 THS 摘要快照（源算值，标注 _ths 后缀），非系统计算",
        ],
        "coverage": {
            "n_periods": len(periods),
            "bs_missing_periods": n_bs_missing,
            "cf_missing_periods": n_cf_missing,
        },
    }


# ---------------------------------------------------------------- 段 4 附加：隐含回报区间表

def _implied_returns(forecasts: dict, valuation_scale: dict,
                     market: dict) -> dict:
    """一致预期 EPS × PE 历史分位 → 隐含价区（替代 DCF；全部 Python 计算）。

    FY1/FY2 归母净利一致预期 ÷ 最新股本 = 预期 EPS；× pe_ttm 分位数
    （p25/p50/p75）= 隐含价区。假设与样本区间强制标注。
    """
    np_f = (forecasts.get("net_profit") or {})
    shares = _dec(market.get("shares_issued"))
    close = _dec(str(market.get("close_raw")) if market.get("close_raw") is not None
                 else None)
    quant = valuation_scale.get("pe_ttm_quantiles") or {}
    out = {
        "note": "隐含价 = 一致预期 FY 归母净利 ÷ 最新股本 × PE(TTM) 历史分位；"
                "非 DCF、非目标价，只是『历史估值刻度 × 券商一致预期』的机械映射",
        "sample_window": valuation_scale.get("sample_window"),
        "rows": [],
    }
    if shares is None or close is None:
        out["status"] = "incomplete: 缺股本或现价"
        return out
    for fy in ("fy1", "fy2"):
        np_v = _dec(np_f.get(fy))
        if np_v is None:
            out["rows"].append({"fy": fy, "status": "incomplete: 无一致预期"})
            continue
        eps = np_v / shares
        row = {"fy": fy, "fy_year": forecasts.get("fy1_year") and
               forecasts["fy1_year"] + (0 if fy == "fy1" else 1),
               "net_profit_forecast": str(np_v), "eps_forecast": _r4(float(eps))}
        for p in ("p25", "p50", "p75"):
            pe = quant.get(p)
            if pe is None:
                row[p] = None
                continue
            price = eps * Decimal(str(pe))
            row[p] = {"pe": pe, "implied_price": _r4(float(price)),
                      "upside_vs_close": _r6(float(price / close - 1))}
        out["rows"].append(row)
    out["status"] = "ok"
    return out


# ---------------------------------------------------------------- 段 5：池内横截面对比

def _pool_comps(conn: sqlite3.Connection, symbol: str) -> dict:
    comps = []
    for wl in conn.execute(
            "SELECT symbol, name, industry_code FROM watchlist WHERE active = 1 "
            "ORDER BY symbol"):
        sym = wl["symbol"]
        pe = conn.execute(
            "SELECT trade_date, pe_ttm FROM indicators_daily "
            "WHERE symbol = ? AND pe_ttm IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 1", (sym,)).fetchone()
        fin_rows = _latest_report_rows(conn, sym)
        latest = fin_rows[-1] if fin_rows else None
        np_yoy = None
        if latest is not None:
            prev = next((r for r in fin_rows
                         if r["period_type"] == latest["period_type"]
                         and r["period_end"][5:] == latest["period_end"][5:]
                         and r["fiscal_year"] == latest["fiscal_year"] - 1), None)
            np_yoy = _yoy(_dec(latest["net_profit_attr"]),
                          _dec(prev["net_profit_attr"]) if prev else None)
        bs_latest = next((r for r in reversed(fin_rows)
                          if r["total_assets"] is not None), None)
        comps.append({
            "symbol": sym, "name": wl["name"],
            "industry_code": wl["industry_code"],
            "is_self": sym == symbol,
            "pe_ttm": _r4(pe["pe_ttm"]) if pe else None,
            "pe_date": pe["trade_date"] if pe else None,
            "latest_period": latest["period_end"] if latest else None,
            "net_profit_yoy": np_yoy,
            "roe": (_div(_dec(bs_latest["net_profit_attr"]),
                         _dec(bs_latest["total_equity_attr"]))
                    if bs_latest else None),
            "asset_liability_ratio": (
                _div(_dec(bs_latest["total_liabilities"]),
                     _dec(bs_latest["total_assets"])) if bs_latest else None),
        })
    return {
        "stocks": comps,
        "note": "池内横截面对比（watchlist 全池）；池外对标属 skill 定性层，"
                "须标注『非系统数据，待人工核』",
    }


# ---------------------------------------------------------------- 段 6：事件与日历

def _events_summary(conn: sqlite3.Connection, symbol: str, as_of: str) -> dict:
    events = []
    for r in conn.execute(
            """
            SELECT e.event_id, e.title, e.event_type, e.source_tier, e.available_at
            FROM events e
            JOIN event_symbols es ON es.event_id = e.event_id
            WHERE es.symbol = ? AND substr(e.available_at, 1, 10) <= ?
            ORDER BY e.available_at DESC LIMIT 10
            """, (symbol, as_of)):
        eff = event_link.resolve_effective(conn, r["event_id"])
        view = eff["symbols"].get(symbol, {})
        events.append({
            "event_id": r["event_id"], "title": r["title"],
            "event_type": r["event_type"], "source_tier": r["source_tier"],
            "available_at": r["available_at"],
            "assessed": eff["status"] is not None,
            "status": view.get("status") or eff["status"],
            "hidden": view.get("hidden", False),
            "direction": view.get("direction") or eff["direction"],
            "materiality": view.get("materiality") or eff["materiality"],
            "rationale": view.get("rationale") or eff["rationale"],
            "narrative": view.get("narrative"),
            "falsification": view.get("falsification") or eff["falsification"],
        })
    pending = conn.execute(
        """
        SELECT COUNT(*) FROM events e
        JOIN event_symbols es ON es.event_id = e.event_id
        WHERE es.symbol = ? AND e.event_type IN ('announcement', 'news')
          AND substr(e.available_at, 1, 10) <= ?
          AND NOT EXISTS (SELECT 1 FROM event_assessments a
                          WHERE a.event_id = e.event_id AND a.symbol = '__event__'
                            AND a.assessment_version = 'llm_v1')
        """, (symbol, as_of)).fetchone()[0]
    horizon = (date.fromisoformat(as_of) + timedelta(days=30)).isoformat()
    calendar = [dict(r) for r in conn.execute(
        """
        SELECT cal_id, kind, symbol, scheduled_date, note FROM event_calendar
        WHERE (symbol = ? OR symbol IS NULL)
          AND scheduled_date BETWEEN ? AND ?
        ORDER BY scheduled_date
        """, (symbol, as_of, horizon))]
    return {
        "as_of": as_of,
        "recent_events": events,
        "pending_unevaluated_count": pending,
        "upcoming_calendar_30d": calendar,
        "note": "标签为 llm_v1 + 人审有效状态（resolve_effective）；"
                "未评价事件不计入但计数公开",
    }


# ---------------------------------------------------------------- 顶层

def build_inputs(conn: sqlite3.Connection, symbol: str) -> dict:
    """构建八段底稿 dict（纯读取）。"""
    try:
        meta, wl = card_inputs._meta(conn, symbol)
    except card_inputs.CardInputsError as exc:
        raise FundamentalInputsError(str(exc)) from exc
    meta["schema"] = SCHEMA
    share_events = valuation.load_share_events(conn, symbol)
    shares = valuation.shares_at(share_events, meta["data_cutoff"]["daily_bars"])
    earnings = card_inputs._earnings(conn, symbol, shares)
    forecasts = card_inputs._forecasts(conn, symbol, earnings)
    try:
        valuation_scale = card_inputs._valuation_scale(conn, symbol)
    except card_inputs.CardInputsError:
        valuation_scale = {
            "panic_lows": [], "current_pe_ttm": None,
            "pe_ttm_quantiles": {f"p{p}": None
                                 for p in card_inputs.QUANTILE_PS},
            "sample_window": None,
            "note": "indicators_daily 无 pe_ttm（§2.5 按缺失标注）",
        }
    market = card_inputs._market_snapshot(conn, symbol, shares)
    ind_mapping, _ind_hash = factor_watch.load_industry_factors()
    factor_snapshot = card_inputs._factor_snapshot(
        conn, symbol, wl, meta["data_cutoff"]["daily_bars"], ind_mapping)

    financials = _financials_multi_year(conn, symbol)
    comps = _pool_comps(conn, symbol)
    events = _events_summary(conn, symbol, meta["data_cutoff"]["daily_bars"])
    implied = _implied_returns(forecasts, valuation_scale, market)

    gaps = []
    if forecasts.get("note"):
        gaps.append(f"forecasts: {forecasts['note']}")
    if financials["coverage"]["bs_missing_periods"]:
        gaps.append(f"financials: {financials['coverage']['bs_missing_periods']}"
                    f" 个期次缺资产负债表")
    if financials["coverage"]["cf_missing_periods"]:
        gaps.append(f"financials: {financials['coverage']['cf_missing_periods']}"
                    f" 个期次缺现金流量表")
    if implied.get("status", "ok") != "ok":
        gaps.append(f"valuation.implied_returns: {implied['status']}")
    if events["pending_unevaluated_count"]:
        gaps.append(f"events: {events['pending_unevaluated_count']} 条公告/新闻未评价")
    if factor_snapshot.get("note") and not factor_snapshot.get("factors"):
        gaps.append(f"factor_snapshot: {factor_snapshot['note']}")

    return {
        "meta": meta,
        "financials_multi_year": financials,
        "forecasts": forecasts,
        "valuation": {**valuation_scale, "market": market,
                      "implied_returns": implied},
        "pool_comps": comps,
        "events_summary": events,
        "factor_snapshot": factor_snapshot,
        "gaps": gaps,
    }


def export_inputs(conn: sqlite3.Connection, symbol: str,
                  out_dir: Path | None = None) -> tuple[dict, Path]:
    """构建底稿并写 `reports/{symbol}/fundamental_inputs_{最新交易日}.json`。"""
    doc = build_inputs(conn, symbol)
    out_dir = out_dir or (ROOT / "reports" / symbol)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"fundamental_inputs_{doc['meta']['data_cutoff']['daily_bars']}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return doc, path


def _summary(doc: dict, path: Path) -> str:
    fin = doc["financials_multi_year"]
    latest = fin["periods"][-1] if fin["periods"] else None
    m = doc["meta"]["data_cutoff"]
    lines = [
        f"{doc['meta']['symbol']}（{doc['meta']['name']}）基本面底稿 → {path}",
        f"数据截止: 行情 {m['daily_bars']} / 财报 {m['financial_reports']}"
        f" / 一致预期 {doc['forecasts'].get('snapshot_at') or '—'}",
        f"财务覆盖: {fin['coverage']['n_periods']} 期"
        f"（缺 BS {fin['coverage']['bs_missing_periods']} /"
        f" 缺 CF {fin['coverage']['cf_missing_periods']}）",
    ]
    if latest:
        d = latest["derived"]
        lines.append(
            f"最新期 {latest['period_end']}: 营收同比 {latest['revenue_yoy']}"
            f" 归母净利同比 {latest['net_profit_yoy']} ROE {d['roe']}"
            f" 资产负债率 {d['asset_liability_ratio']} FCF {d['fcf']}")
    ir = doc["valuation"]["implied_returns"]
    if ir.get("status") == "ok" and ir["rows"]:
        r0 = ir["rows"][0]
        if r0.get("p50"):
            lines.append(
                f"隐含回报（FY1 × PE p25/p50/p75）: "
                f"{r0['p25']['implied_price']} / {r0['p50']['implied_price']} / "
                f"{r0['p75']['implied_price']}（现价 {doc['valuation'].get('current_pe_ttm')} PE）")
    if doc["gaps"]:
        lines.append("gaps: " + "；".join(doc["gaps"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.fundamental_inputs")
    parser.add_argument("symbol")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--out-dir", default=None,
                        help="输出目录（默认 reports/{symbol}/）")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        doc, path = export_inputs(conn, args.symbol,
                                  Path(args.out_dir) if args.out_dir else None)
        print(_summary(doc, path))
        return 0
    except FundamentalInputsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
