"""TTM 归母净利与 PE(TTM)（D1.7，设计 §3.7、§4.1）。

口径：
- TTM 在任意 as_of 上按当时可见（available_at <= as_of）的最新修订计算：
    最新报告为年报 → TTM = 最新年报归母净利；
    最新报告为本财年累计中报/季报 →
        TTM = 上一财年年报 + 本财年最新累计 − 上一财年同期累计。
  三个组成项都必须在 as_of 时可见，任一缺失 → TTM 为空（不补历史空洞）。
- pe_ttm = close_raw × 当日已生效股本 ÷ TTM 归母净利（不复权市值口径，§4.1）。
  股本取 share_capital_events 中 effective_at <= as_of 的最新事件；同一 effective_at
  有多口径记录时优先 group_total（A+H 集团总股本，vendor 通用口径），无则回退 issued。
  股本可见性为快照豁免混合口径：event_type 以 snapshot_ 开头的单点快照行豁免
  available_at 过滤（§3.7 单点假设，details_json 已标注），其余真实事件行要求
  available_at <= as_of（点时过滤，消除前视）；所选股本来自快照豁免路径时
  pe_status 追加 ";snapshot_share_basis" 标注。
- TTM<=0、股本缺失、币种不一致且缺汇率 → PE 为空并保存原因码（pe_status）。
- 财务金额为关键决策值（TEXT 定点），内部用 Decimal 运算，写库展示值转 float。

股本快照落盘：yahoo_finance get_stock_info 只有当前快照（无历史事件流），
按 §3.7 降级作为 share_capital_events 单点事件写入，share_count_type=issued，
details_json 标注来源与覆盖假设（整个保留区间股本不变，后续需交叉验证）。
2026-08-17 起 A/H 双上市公司改用 stock_finance_data get_stock_info 的
ths_total_shares_stock 写 share_count_type=group_total 单点快照（A+H 全口径），
与旧 issued 快照并存，PE 计算优先取 group_total（§3.7）。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.adapters.common import sha256_file
from scripts.pipeline.db import utc_now

# pe_status 原因码（成功为 "ok"，可带 ";..." 降级标注）
S_OK = "ok"
S_NO_SHARE = "no_share_capital"            # 股本缺失
S_NO_REPORT = "no_visible_report"          # as_of 时无可见财报
S_TTM_NON_POSITIVE = "ttm_non_positive"    # TTM <= 0
S_MISSING_PREV_ANNUAL = "ttm_missing_prev_annual"          # 缺上一财年年报
S_MISSING_PREV_SAME = "ttm_missing_prev_same_period"       # 缺上一财年同期累计
S_MISSING_NET_PROFIT = "ttm_missing_net_profit"            # 报告无归母净利
S_FX_MISSING = "fx_missing"                # 币种不一致且缺汇率

DEGRADED_AVAILABLE_AT = "degraded_available_at"  # 财报 available_at 为入库时间降级
SNAPSHOT_SHARE_BASIS = "snapshot_share_basis"    # 股本取自单点快照豁免路径（§3.7 假设）


@dataclass
class ReportView:
    """财报点时视图（net_profit_attr 定点）。"""

    period_end: str          # YYYY-MM-DD
    period_type: str         # annual / interim / quarterly
    fiscal_year: int
    is_cumulative: bool
    net_profit_attr: Decimal | None
    available_at: str        # UTC ISO
    revision: int = 1
    currency: str | None = None


@dataclass
class ShareEventView:
    effective_at: str        # 市场本地日期
    available_at: str        # UTC
    shares: Decimal
    share_count_type: str    # issued / float / group_total（A+H 集团总股本）
    event_type: str = ""     # snapshot_* 为单点快照，豁免 available_at 点时过滤（§3.7）


# ---------------------------------------------------------------- TTM（纯函数，golden tests 锁定）

def _latest_revisions(reports: list[ReportView]) -> list[ReportView]:
    """同一报告期只保留最新 revision（不做时点过滤）。"""
    latest: dict[tuple, ReportView] = {}
    for r in reports:
        key = (r.period_end, r.period_type, r.is_cumulative)
        if key not in latest or r.revision > latest[key].revision:
            latest[key] = r
    return sorted(latest.values(), key=lambda r: r.period_end)


def visible_reports(reports: list[ReportView], as_of: datetime) -> list[ReportView]:
    """点时过滤：available_at <= as_of；同一报告期只保留最新 revision。"""
    vis = [r for r in reports if _parse_ts(r.available_at) <= as_of]
    return _latest_revisions(vis)


def ttm_net_profit(reports: list[ReportView]) -> tuple[Decimal | None, str]:
    """对**已点时过滤**的报告集计算 TTM 归母净利（§3.7）。返回 (值, 原因码)。"""
    if not reports:
        return None, S_NO_REPORT
    latest = max(reports, key=lambda r: (r.period_end, r.fiscal_year))
    if latest.period_type == "annual":
        if latest.net_profit_attr is None:
            return None, S_MISSING_NET_PROFIT
        return latest.net_profit_attr, S_OK

    # 本财年累计中报/季报：上年年报 + 本年累计 − 去年同期累计
    fy = latest.fiscal_year
    mmdd = latest.period_end[5:]
    prev_annual = _find(reports, period_type="annual", fiscal_year=fy - 1)
    prev_same = _find(reports, period_type=latest.period_type, fiscal_year=fy - 1,
                      period_mmdd=mmdd, is_cumulative=True)
    if prev_annual is None:
        return None, S_MISSING_PREV_ANNUAL
    if prev_same is None:
        return None, S_MISSING_PREV_SAME
    vals = (latest.net_profit_attr, prev_annual.net_profit_attr, prev_same.net_profit_attr)
    if any(v is None for v in vals):
        return None, S_MISSING_NET_PROFIT
    return prev_annual.net_profit_attr + latest.net_profit_attr - prev_same.net_profit_attr, S_OK


def _find(reports: list[ReportView], *, period_type: str, fiscal_year: int,
          period_mmdd: str | None = None, is_cumulative: bool | None = None) -> ReportView | None:
    for r in reports:
        if r.period_type != period_type or r.fiscal_year != fiscal_year:
            continue
        if period_mmdd is not None and r.period_end[5:] != period_mmdd:
            continue
        if is_cumulative is not None and r.is_cumulative != is_cumulative:
            continue
        return r
    return None


# ---------------------------------------------------------------- PE（纯函数）

def pe_ttm(close_raw: float, shares: Decimal | None, ttm: Decimal | None,
           ttm_reason: str, *, fx_rate: Decimal | None = None,
           fx_needed: bool = False) -> tuple[float | None, str]:
    """pe_ttm = close_raw × 股本 ÷ (TTM × 汇率)。返回 (pe, pe_status)。"""
    if shares is None or shares <= 0:
        return None, S_NO_SHARE
    if ttm is None:
        return None, ttm_reason
    if ttm <= 0:
        return None, S_TTM_NON_POSITIVE
    ttm_traded = ttm
    if fx_needed:
        if fx_rate is None:
            return None, S_FX_MISSING
        ttm_traded = ttm * fx_rate
    return float(Decimal(str(close_raw)) * shares / ttm_traded), S_OK


# 同一 effective_at 下股本口径优先级：group_total（A+H 集团总股本）> 其他
_SHARE_TYPE_PRIORITY = {"group_total": 1}


def _is_snapshot_event(e: ShareEventView) -> bool:
    """event_type 以 snapshot_ 开头的单点快照行，豁免 available_at 点时过滤（§3.7 单点假设）。"""
    return e.event_type.startswith("snapshot_")


def _select_share_event(events: list[ShareEventView], as_of_date: str,
                        as_of_ts: datetime) -> ShareEventView | None:
    """点时选择 as_of 当日股本事件（快照豁免混合口径）。

    候选条件：effective_at <= as_of_date，且（snapshot_* 快照行豁免，或
    available_at <= as_of_ts）。同一 effective_at 存在多口径记录时优先
    group_total（A/H 双上市公司 PE 分母用集团总股本，vendor 通用口径），
    无则回退 issued 等其余口径（纯 A 股股票行为不变）。
    """
    valid = [
        e for e in events
        if e.effective_at <= as_of_date
        and (_is_snapshot_event(e) or _parse_ts(e.available_at) <= as_of_ts)
    ]
    if not valid:
        return None
    return max(valid, key=lambda e: (
        e.effective_at, _SHARE_TYPE_PRIORITY.get(e.share_count_type, 0)))


def shares_at(events: list[ShareEventView], as_of_date: str,
              as_of_ts: datetime | None = None) -> Decimal | None:
    """as_of 当日已生效的最新股本（口径见 _select_share_event）。

    as_of_ts 为点时过滤用的 as_of 时刻（UTC）；缺省取 as_of_date 当日 UTC 末。
    """
    if as_of_ts is None:
        as_of_ts = datetime.combine(
            date.fromisoformat(as_of_date), datetime.max.time(), tzinfo=timezone.utc)
    ev = _select_share_event(events, as_of_date, as_of_ts)
    return ev.shares if ev is not None else None


# ---------------------------------------------------------------- DB 读取

def load_reports(conn: sqlite3.Connection, symbol: str) -> list[ReportView]:
    rows = conn.execute(
        """
        SELECT r.period_end, r.period_type, r.fiscal_year, r.is_cumulative,
               r.available_at, r.revision, r.currency, f.net_profit_attr
        FROM financial_reports r
        LEFT JOIN financial_facts f ON f.report_id = r.report_id
        WHERE r.symbol = ?
        ORDER BY r.period_end
        """,
        (symbol,),
    ).fetchall()
    return [
        ReportView(
            period_end=r["period_end"], period_type=r["period_type"],
            fiscal_year=r["fiscal_year"], is_cumulative=bool(r["is_cumulative"]),
            net_profit_attr=Decimal(r["net_profit_attr"]) if r["net_profit_attr"] else None,
            available_at=r["available_at"], revision=r["revision"], currency=r["currency"],
        )
        for r in rows
    ]


def load_share_events(conn: sqlite3.Connection, symbol: str) -> list[ShareEventView]:
    rows = conn.execute(
        """
        SELECT effective_at, available_at, shares_issued_after, share_count_type, event_type
        FROM share_capital_events WHERE symbol = ? ORDER BY effective_at
        """,
        (symbol,),
    ).fetchall()
    return [
        ShareEventView(
            effective_at=r["effective_at"], available_at=r["available_at"],
            shares=Decimal(r["shares_issued_after"]),
            share_count_type=r["share_count_type"] or "issued",
            event_type=r["event_type"] or "",
        )
        for r in rows
        if r["shares_issued_after"]
    ]


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_pe_series(
    conn: sqlite3.Connection,
    symbol: str,
    dates: list[str],
    close_raw: dict[str, float],
    market_tz: str,
    trade_currency: str,
    *,
    assume_visible: bool = False,
) -> dict[str, tuple[float | None, str]]:
    """逐日 PE(TTM)。返回 {trade_date: (pe_ttm|None, pe_status)}。

    as_of = 该市场当地日期 23:59:59（转 UTC）。
    assume_visible：财报 available_at 为入库时间降级时照常使用报告
    （D1.3 记录的已知降级），但同一报告期仍只取最新 revision（旧 revision
    的占位/NULL 行不参与 TTM），pe_status 追加 ";degraded_available_at" 标注。
    股本按快照豁免混合口径选择（_select_share_event）：所选股本来自
    snapshot_* 单点快照时 pe_status 追加 ";snapshot_share_basis" 标注。
    """
    reports = load_reports(conn, symbol)
    share_events = load_share_events(conn, symbol)
    degraded = assume_visible and reports
    out: dict[str, tuple[float | None, str]] = {}
    tz = ZoneInfo(market_tz)
    for d in dates:
        as_of = datetime.combine(date.fromisoformat(d), datetime.max.time(), tzinfo=tz)
        as_of_utc = as_of.astimezone(timezone.utc)
        vis = _latest_revisions(reports) if assume_visible else visible_reports(reports, as_of_utc)
        ttm, reason = ttm_net_profit(vis)
        share_ev = _select_share_event(share_events, d, as_of_utc)
        shares = share_ev.shares if share_ev is not None else None
        fin_ccy = next((r.currency for r in vis if r.currency), None)
        fx_needed = bool(fin_ccy and trade_currency and fin_ccy != trade_currency)
        # 第一版 fx_rates 取最近可用日汇率的兜底在 adapter 层；此处币种一致不需换算，
        # 不一致时按 fx_missing 返回空值（§3.7：缺汇率不计算 PE）
        pe, status = pe_ttm(close_raw[d], shares, ttm, reason, fx_needed=fx_needed)
        if share_ev is not None and _is_snapshot_event(share_ev):
            status = f"{status};{SNAPSHOT_SHARE_BASIS}"
        if degraded:
            status = f"{status};{DEGRADED_AVAILABLE_AT}"
        out[d] = (pe, status)
    return out


# ---------------------------------------------------------------- 股本快照入库（yahoo get_stock_info 降级来源，§3.7）

def load_share_snapshot(
    conn: sqlite3.Connection,
    symbol: str,
    csv_path: str | Path,
    *,
    effective_at: str,
    run_id: str,
) -> dict:
    """解析 yahoo_finance get_stock_info CSV 的总股本快照并写入 share_capital_events。

    快照只有当前值：作为单点事件写入，effective_at 取覆盖区间起点（整个保留区间
    按此股本计算，details_json 标注假设）；share_count_type=issued（sharesOutstanding
    为已发行总股本，floatShares 仅记入 details）。已存在同 effective_at/source/
    share_count_type 的事件且股本一致时跳过（幂等）；冲突校验只在同 share_count_type
    内比对（与 group_total 口径快照并存不视为冲突）。
    """
    csv_path = Path(csv_path)
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{csv_path} 无数据行")
    rec = rows[0]
    shares_out = (rec.get("sharesOutstanding") or "").strip()
    if not shares_out:
        raise ValueError(f"{csv_path} 无 sharesOutstanding 字段值")
    shares = Decimal(shares_out)
    float_shares = (rec.get("floatShares") or "").strip()

    content_hash = sha256_file(csv_path)
    now = utc_now()
    raw_object_id = f"raw_yahoo_stock_info_{content_hash[:12]}"
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_objects (raw_object_id, run_id, source, data_type,
            symbol, request_params_json, file_path, content_hash, fetch_status, ingested_at)
        VALUES (?, ?, 'yahoo_finance', 'stock_info', ?, ?, ?, ?, 'ok', ?)
        """,
        (raw_object_id, run_id, symbol,
         json.dumps({"api": "get_stock_info", "ticker_csv": csv_path.name}, ensure_ascii=False),
         str(csv_path), content_hash, now),
    )

    details = {
        "snapshot": "yahoo_finance get_stock_info sharesOutstanding（单点快照，无历史事件流）",
        "assumption": f"整个保留区间（自 {effective_at} 起）股本按此值；增发/回购需后续用 "
                      "get_stock_actions 与 financial_facts 期末股数交叉验证（§3.7 来源②③）",
        "floatShares": float_shares or None,
        "raw_object_id": raw_object_id,
    }
    existing = conn.execute(
        """
        SELECT sce_id, shares_issued_after FROM share_capital_events
        WHERE symbol = ? AND effective_at = ? AND source = 'yahoo_finance get_stock_info'
          AND share_count_type = 'issued'
        """,
        (symbol, effective_at),
    ).fetchone()
    if existing is not None:
        if Decimal(existing["shares_issued_after"]) == shares:
            return {"inserted": False, "shares": str(shares), "sce_id": existing["sce_id"],
                    "raw_object_id": raw_object_id}
        raise ValueError(
            f"股本冲突：已有 {existing['shares_issued_after']}，新快照 {shares}（§3.2 数据冲突）")
    cur = conn.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at, event_type,
            share_change, shares_issued_after, share_count_type, details_json, source,
            raw_object_id, created_at)
        VALUES (?, ?, ?, 'snapshot_issued', NULL, ?, 'issued', ?,
                'yahoo_finance get_stock_info', ?, ?)
        """,
        (symbol, effective_at, now, str(shares),
         json.dumps(details, ensure_ascii=False), raw_object_id, now),
    )
    return {"inserted": True, "shares": str(shares), "sce_id": cur.lastrowid,
            "raw_object_id": raw_object_id}


# ------------------------------------------------- group_total 快照入库（stock_finance_data，§3.7）

def load_group_total_snapshot(
    conn: sqlite3.Connection,
    symbol: str,
    csv_path: str | Path,
    *,
    effective_at: str,
    run_id: str,
    source: str = "stock_finance_data",
    api_label: str = "get_stock_info",
    raw_prefix: str = "raw_ths_stock_info",
) -> dict:
    """解析集团总股本（A+H 全口径）快照 CSV，以 share_count_type='group_total'
    写入 share_capital_events。

    ths_total_shares_stock 为 A+H 集团总股本（vendor 通用 PE 股本口径）。
    CSV 可为多股票批量文件，按 thscode 列定位 symbol 对应行。快照只有当前值：
    作为单点事件写入（event_type='snapshot_group_total'），effective_at 取覆盖区间
    起点，details_json 标注单点快照假设（H 股上市/增发/回购前的历史区间按当前
    总股本计算，存在口径偏差，后续可用 get_stock_actions 细化）。
    已存在同 effective_at/source/share_count_type 的事件且股本一致时跳过（幂等）；
    同类型股本不一致抛"股本冲突"，与 issued 等其他口径并存不视为冲突。

    source/api_label/raw_prefix 供对齐同列约定的可选源复用（如 akshare
    stock_zh_a_gbjg_em，source='akshare'），默认保持 stock_finance_data 行为不变。
    """
    source_label = f"{source} {api_label}"
    csv_path = Path(csv_path)
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rec = next((r for r in rows if (r.get("thscode") or "").strip() == symbol), None)
    if rec is None:
        raise ValueError(f"{csv_path} 无 {symbol} 对应行（thscode 列）")
    shares_raw = (rec.get("ths_total_shares_stock") or "").strip()
    if not shares_raw:
        raise ValueError(f"{csv_path} {symbol} 无 ths_total_shares_stock 字段值（§2.5 不猜）")
    shares = Decimal(shares_raw)
    if shares != shares.to_integral_value():
        raise ValueError(f"{csv_path} {symbol} 总股本非整数：{shares_raw}")
    shares = shares.to_integral_value()

    content_hash = sha256_file(csv_path)
    now = utc_now()
    raw_object_id = f"{raw_prefix}_{symbol.replace('.', '')}_{content_hash[:12]}"
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_objects (raw_object_id, run_id, source, data_type,
            symbol, request_params_json, file_path, content_hash, fetch_status, ingested_at)
        VALUES (?, ?, ?, 'stock_info', ?, ?, ?, ?, 'ok', ?)
        """,
        (raw_object_id, run_id, source, symbol,
         json.dumps({"api": api_label, "field": "ths_total_shares_stock",
                     "ticker_csv": csv_path.name}, ensure_ascii=False),
         str(csv_path), content_hash, now),
    )

    details = {
        "snapshot": f"{source_label} ths_total_shares_stock"
                    "（A+H 集团总股本，单点快照，无历史事件流）",
        "assumption": f"整个保留区间（自 {effective_at} 起）集团总股本按此值；H 股上市/"
                      "增发/回购注销前的历史区间存在口径偏差，需后续用 get_stock_actions "
                      "与 financial_facts 期末股数交叉验证细化（§3.7 来源②③）",
        "raw_object_id": raw_object_id,
    }
    existing = conn.execute(
        """
        SELECT sce_id, shares_issued_after FROM share_capital_events
        WHERE symbol = ? AND effective_at = ?
          AND source = ?
          AND share_count_type = 'group_total'
        """,
        (symbol, effective_at, source_label),
    ).fetchone()
    if existing is not None:
        if Decimal(existing["shares_issued_after"]) == shares:
            return {"inserted": False, "shares": str(shares), "sce_id": existing["sce_id"],
                    "raw_object_id": raw_object_id}
        raise ValueError(
            f"股本冲突：已有 group_total {existing['shares_issued_after']}，"
            f"新快照 {shares}（§3.2 数据冲突）")
    cur = conn.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at, event_type,
            share_change, shares_issued_after, share_count_type, details_json, source,
            raw_object_id, created_at)
        VALUES (?, ?, ?, 'snapshot_group_total', NULL, ?, 'group_total', ?,
                ?, ?, ?)
        """,
        (symbol, effective_at, now, str(shares),
         json.dumps(details, ensure_ascii=False), source_label, raw_object_id, now),
    )
    return {"inserted": True, "shares": str(shares), "sce_id": cur.lastrowid,
            "raw_object_id": raw_object_id}
