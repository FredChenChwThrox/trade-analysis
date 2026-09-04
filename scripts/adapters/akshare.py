"""akshare adapter（可选数据源）：解析 akshare 采集落盘 CSV → 入库。

akshare 采集器（scripts/collect/akshare_collect.py）落盘列与既有 adapter 约定对齐，
本模块尽可能复用已验证的解析/入库逻辑：
- price → 复用 stock_finance_data.upsert_daily_bars（source=akshare）
- index → 复用 stock_finance_data.upsert_index_bars（source=akshare）
- financials → 直接转发 tdx.parse_financials_csv（列约定一致，含 published_at/下一开市日/单位换算/修订）
- announcement → 委托公共引擎 announcements.parse_disclosure_csv（标准公告线格式，
  events.source='akshare' 与 tdx dedup 命名空间隔离，§3.6）；
  注意：采集器暂无 cninfo 抓取源，入库通道仅面向已落盘 CSV
- forecast → 转发 stock_finance_data.parse_forecast_csv（列约定一致，source=akshare）
- stock_info → share_capital_events（snapshot_group_total/group_total，复用
  indicators.valuation.load_group_total_snapshot；与 kimi 源可切换：同 effective_at
  已有其他来源同股本快照幂等跳过，股本不一致记 conflict）
- telegraph → events + event_symbols（source_external_id/content_hash 去重，§3.6；
  股票关联按 watchlist 名称/别名/symbol 匹配；source_tier=4 财经媒体，r2 §2.1）
- balance_sheet / cash_flow（sina 全历史，基本面分析数据层 2026-09-03 新增）→
  balance_sheet_facts / cash_flow_facts；表头复用 financial_reports
  （2023 前期次新建表头，published_at=NULL、available_at=入库时间降级，
  仅服务长期趋势分析，不进信号链）
- fin_abstract（THS 财务摘要）→ financial_indicator_snapshots（payload 快照，
  交叉核对用）；并对存量 income facts 做 0.5% 容差对账，2023 前缺失期次
  回填 financial_facts（revenue/net_profit_attr/eps_basic，源=THS 摘要）

口径（对齐库 schema，§3.2/§9.5）：
- 成交量已在采集器层 ×100 换为「股」；成交额「元」直接入库；
- 财报金额 unit='yuan'，由 tdx 复用逻辑校验；published_at 来自 NOTICE_DATE 披露日。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scripts.adapters import announcements
from scripts.adapters.common import (
    IngestResult,
    dec_str,
    load_calendar,
    market_of,
    market_tz,
    record_revision,
    symbol_from_code_setcode,
    utc_now,
)
from scripts.adapters.stock_finance_data import (
    _num,
    _validate_bar_row,
    parse_forecast_csv as _sfd_parse_forecast,
    upsert_daily_bars,
    upsert_index_bars,
    vals_equal,
)
from scripts.adapters.tdx import (
    _FIN_PERIOD_TYPE,
    parse_financials_csv as _tdx_parse_financials,
)
from scripts.indicators import valuation

SOURCE = "akshare"

# stock_info 快照入库用的来源标签（share_capital_events.source / raw_objects.source）
_STOCK_INFO_API = "stock_zh_a_gbjg_em"

# events 去重来源外部 ID 前缀（与采集器 source_external_id 约定一致）
_SYMBOL_RE = re.compile(r"\b(\d{6}\.(?:SH|SZ|BJ|HK))\b")


def parse_price_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """akshare 行情 CSV → daily_bars（列：thscode,time,open,high,low,close,volume,amount,currency）。"""
    calendar_cache: dict[str, dict] = {}
    bars: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for rec in csv.DictReader(f):
            symbol = (rec.get("thscode") or "").strip()
            if not symbol:
                result.conflicts += 1
                result.errors.append("缺少 thscode 列或值为空")
                return result
            raw_time = (rec.get("time") or "").strip()
            try:
                trade_date = datetime.strptime(raw_time, "%Y%m%d").date().isoformat()
            except ValueError:
                result.conflicts += 1
                result.errors.append(f"time 列无法解析: {raw_time!r}")
                return result
            market = market_of(symbol)
            if market not in calendar_cache:
                calendar_cache[market] = load_calendar(conn, market)
            calendar = calendar_cache[market]
            if not calendar:
                reason = f"trading_calendar 缺失（market={market}），交易日校验跳过"
                if reason not in result.incomplete_reasons:
                    result.incomplete_reasons.append(reason)
            else:
                cal = calendar.get(trade_date)
                if cal is None:
                    result.conflicts += 1
                    result.errors.append(
                        f"{trade_date} 不在 trading_calendar 种子范围内（market={market}）")
                    return result
                if not cal["is_open"]:
                    result.conflicts += 1
                    result.errors.append(
                        f"{trade_date} 非交易日（{cal['status']}）却有行情 bar（{symbol}）")
                    return result
            row = {
                "open": _num(rec.get("open")), "high": _num(rec.get("high")),
                "low": _num(rec.get("low")), "close": _num(rec.get("close")),
                "volume": _num(rec.get("volume")), "amount": _num(rec.get("amount")),
                "turnover": _num(rec.get("turnover")),
            }
            bad = _validate_bar_row(row, trade_date)
            if bad == "EMPTY":
                result.skipped += 1
                result.notes.append(f"{trade_date} OHLC 全缺失（残缺 bar），行级跳过")
                continue
            if bad:
                result.conflicts += 1
                result.errors.append(bad)
                return result
            bars.append({
                "symbol": symbol, "trade_date": trade_date, "market": market,
                **row, "currency": (rec.get("currency") or "").strip() or None,
            })
    run_id = Path(path).parent.name
    upsert_daily_bars(conn, bars, source=SOURCE, raw_object_id=raw_object_id,
                      run_id=run_id, result=result)
    # turnover（migration 0008）：派生快照元数据，差异更新不记 data_revisions
    # （价格事实字段 revision 语义不变）；旧格式 CSV 无该列 → rec.get 为 None 跳过。
    trs = [(b["turnover"], b["symbol"], b["trade_date"])
           for b in bars if b.get("turnover") is not None]
    if trs:
        cur = conn.executemany(
            "UPDATE daily_bars SET turnover=? "
            "WHERE symbol=? AND trade_date=? AND (turnover IS NULL OR turnover != ?)",
            [(t, s, d, t) for t, s, d in trs])
        if cur.rowcount:
            result.notes.append(f"turnover 更新 {cur.rowcount} 行")
    return result


def parse_financials_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                         result: IngestResult) -> IngestResult:
    """akshare 利润表 CSV → financial_reports/facts（复用 tdx 已验证解析）。

    akshare 采集器按 tdx 列约定落盘（code,setcode,period_end,...,published_at），
    其中 published_at = NOTICE_DATE（东财正式披露日），正好补上 A 股披露时间缺口。
    """
    return _tdx_parse_financials(conn, path, raw_object_id, result)


# ------------------------------------------- 资产负债表 / 现金流量表（sina 全历史，仅 A 股）

# 库字段顺序与采集器 _BS_COLS / _CF_COLS 对齐（金额单位元，采集器已实测校验）
_BS_FIELDS = ["total_assets", "total_liabilities", "total_equity_attr",
              "monetary_fund", "short_term_borrowing", "long_term_borrowing",
              "bonds_payable", "noncurrent_liab_1y", "inventory",
              "accounts_receivable", "accounts_payable", "goodwill"]
_CF_FIELDS = ["ocf", "capex", "icf", "financing_cf", "net_cash_increase"]

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _find_or_create_report_header(conn: sqlite3.Connection, *, symbol: str,
                                  period_end: str, currency: str,
                                  raw_object_id: str, run_id: str,
                                  now: str) -> tuple[int, bool]:
    """找 (symbol, period_end, period_type, is_cumulative=1) 最新 revision 表头；
    不存在则新建并返回 created=True。

    新建场景 = 2023 年以前的历史期次（sina 报表无披露日）：published_at=NULL、
    available_at=入库时间（D1.3 同款降级）。这些期次只服务长期趋势分析，
    不进信号链；PIT 方向安全（历史 as-of 查询看不到 available_at=now 的行）。
    """
    mmdd = period_end[5:7] + period_end[8:10]
    period_type = _FIN_PERIOD_TYPE.get(mmdd, "quarterly")
    row = conn.execute(
        """
        SELECT report_id FROM financial_reports
        WHERE symbol = ? AND period_end = ? AND period_type = ? AND is_cumulative = 1
        ORDER BY revision DESC LIMIT 1
        """,
        (symbol, period_end, period_type),
    ).fetchone()
    if row is not None:
        return row["report_id"], False
    cur = conn.execute(
        """
        INSERT INTO financial_reports (symbol, period_end, period_type, fiscal_year,
            published_at, published_tz, available_at, revision,
            currency, unit, is_cumulative, raw_object_id, ingested_at)
        VALUES (?, ?, ?, ?, NULL, NULL, ?, 1, ?, 'yuan', 1, ?, ?)
        """,
        (symbol, period_end, period_type, int(period_end[:4]),
         now, currency, raw_object_id, now),
    )
    record_revision(
        conn, table_name="financial_reports",
        record_key={"symbol": symbol, "period_end": period_end,
                    "period_type": period_type, "is_cumulative": 1, "revision": 1},
        old_value=None, new_value=None, source=SOURCE,
        reason="akshare 报表回填：新建历史期表头（sina 无披露日，available_at 降级）",
        run_id=run_id)
    return cur.lastrowid, True


def _parse_statement_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                         result: IngestResult, *, table: str,
                         fields: list[str], label: str) -> IngestResult:
    """sina 资产负债表/现金流量表 CSV（每期一行）→ 对应 facts 表。

    幂等：内容一致跳过；内容变化原地更新 + data_revisions 记录（报表更正罕见，
    header revision 语义留给利润表）；全空字段期次行级跳过。
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.conflicts += 1
        result.errors.append(f"{label} CSV 无数据行")
        return result
    now = utc_now()
    run_id = Path(path).parent.name
    created_headers = 0
    for rec in rows:
        code = (rec.get("code") or "").strip()
        setcode = (rec.get("setcode") or "").strip()
        period_end = (rec.get("period_end") or "").strip()
        if not code or not setcode or not _PERIOD_RE.match(period_end):
            result.conflicts += 1
            result.errors.append(
                f"{label} 行缺 code/setcode 或 period_end 非法: {dict(rec)!r}"[:200])
            return result
        try:
            symbol = symbol_from_code_setcode(code, setcode)
        except ValueError as e:
            result.conflicts += 1
            result.errors.append(str(e))
            return result
        facts = {f: dec_str(rec.get(f)) for f in fields}
        if all(v is None for v in facts.values()):
            result.skipped += 1
            continue
        currency = (rec.get("currency") or "").strip() or "CNY"
        report_id, created = _find_or_create_report_header(
            conn, symbol=symbol, period_end=period_end, currency=currency,
            raw_object_id=raw_object_id, run_id=run_id, now=now)
        created_headers += created
        existing = conn.execute(
            f"SELECT * FROM {table} WHERE report_id = ?", (report_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                f"INSERT INTO {table} (report_id, {', '.join(fields)}, updated_at) "
                f"VALUES (?, {', '.join('?' * len(fields))}, ?)",
                (report_id, *[facts[f] for f in fields], now),
            )
            result.inserted += 1
        elif all(existing[f] == facts[f] for f in fields):
            result.skipped += 1
        else:
            conn.execute(
                f"UPDATE {table} SET {', '.join(f'{f} = ?' for f in fields)}, "
                f"updated_at = ? WHERE report_id = ?",
                (*[facts[f] for f in fields], now, report_id),
            )
            record_revision(
                conn, table_name=table,
                record_key={"report_id": report_id, "symbol": symbol,
                            "period_end": period_end},
                old_value={f: existing[f] for f in fields}, new_value=facts,
                source=SOURCE, reason=f"{label} 内容更正（原地更新）", run_id=run_id)
            result.updated += 1
    if created_headers:
        result.incomplete_reasons.append(
            f"{label}：新建 {created_headers} 个历史期财报表头（sina 无披露日，"
            f"available_at=入库时间降级，仅服务趋势分析不进信号链）")
    return result


def parse_balance_sheet_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                            result: IngestResult) -> IngestResult:
    """akshare 资产负债表 CSV → balance_sheet_facts。"""
    return _parse_statement_csv(conn, path, raw_object_id, result,
                                table="balance_sheet_facts", fields=_BS_FIELDS,
                                label="资产负债表")


def parse_cash_flow_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                        result: IngestResult) -> IngestResult:
    """akshare 现金流量表 CSV → cash_flow_facts。"""
    return _parse_statement_csv(conn, path, raw_object_id, result,
                                table="cash_flow_facts", fields=_CF_FIELDS,
                                label="现金流量表")


# ------------------------------------------------- THS 财务摘要（快照 + income 对账/回填）

# 常用指标组 → financial_facts 列（金额单位元，与 financial_facts 口径一致，实测）
_ABSTRACT_INCOME = {"归母净利润": "net_profit_attr", "营业总收入": "revenue",
                    "基本每股收益": "eps_basic"}

# income 对账容差（相对偏差）；THS 摘要与 tdx/东财利润表可能存在重述差异
_INCOME_TOLERANCE = Decimal("0.005")


def _rel_diff(a: str | None, b: str | None) -> Decimal | None:
    """相对偏差 |a−b|/|b|；任一缺失或 b=0 返回 None。"""
    if a is None or b is None:
        return None
    da, db = Decimal(a), Decimal(b)
    if db == 0:
        return None
    return abs(da - db) / abs(db)


def parse_fin_abstract_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                           result: IngestResult) -> IngestResult:
    """THS 财务摘要长表 CSV → financial_indicator_snapshots（payload 快照）。

    列：period_end,group,indicator,value（采集器转置落盘；symbol 从文件名
    `{symbol}_abstract.csv` 取）。除快照外顺带做两件事：
    1. 对账：常用指标组的归母净利润/营业总收入与存量 financial_facts 比对
       （容差 0.5%），超差记 incomplete 不静默；
    2. 回填：2023 年以前无 income facts 的期次，用摘要值补 financial_facts
       （无表头则先建降级表头）；已有 facts 的期次绝不动（修订权属 tdx/东财通道）。
    """
    m = re.match(r"(\d{6}\.(?:SH|SZ|BJ))_abstract", Path(path).stem)
    if m is None:
        result.conflicts += 1
        result.errors.append(f"文件名不符合 {{symbol}}_abstract.csv 约定: {path.name}")
        return result
    symbol = m.group(1)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.conflicts += 1
        result.errors.append("财务摘要 CSV 无数据行")
        return result

    periods: dict[str, dict[str, dict[str, str]]] = {}
    for rec in rows:
        pe = (rec.get("period_end") or "").strip()
        group = (rec.get("group") or "").strip()
        indicator = (rec.get("indicator") or "").strip()
        val = dec_str(rec.get("value"))
        if not _PERIOD_RE.match(pe) or not group or not indicator or val is None:
            result.skipped += 1
            continue
        periods.setdefault(pe, {}).setdefault(group, {})[indicator] = val

    now = utc_now()
    run_id = Path(path).parent.name
    created_headers = backfilled_facts = 0
    mismatches: list[str] = []
    for pe, payload in sorted(periods.items()):
        snap = conn.execute(
            "SELECT snapshot_id, payload_json FROM financial_indicator_snapshots "
            "WHERE symbol = ? AND period_end = ? AND source = ?",
            (symbol, pe, SOURCE)).fetchone()
        if snap is not None and json.loads(snap["payload_json"]) == payload:
            result.skipped += 1
        elif snap is not None:
            conn.execute(
                "UPDATE financial_indicator_snapshots "
                "SET payload_json = ?, raw_object_id = ?, ingested_at = ? "
                "WHERE snapshot_id = ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 raw_object_id, now, snap["snapshot_id"]))
            record_revision(
                conn, table_name="financial_indicator_snapshots",
                record_key={"snapshot_id": snap["snapshot_id"], "symbol": symbol,
                            "period_end": pe},
                old_value=snap["payload_json"], new_value=payload,
                source=SOURCE, reason="THS 财务摘要内容更新", run_id=run_id)
            result.updated += 1
        else:
            conn.execute(
                "INSERT INTO financial_indicator_snapshots (symbol, period_end, source,"
                " payload_json, raw_object_id, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, pe, SOURCE,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 raw_object_id, now))
            result.inserted += 1

        common = payload.get("常用指标") or {}
        np_attr = dec_str(common.get("归母净利润"))
        revenue = dec_str(common.get("营业总收入"))
        eps = dec_str(common.get("基本每股收益"))
        if np_attr is None and revenue is None:
            continue
        header = conn.execute(
            """
            SELECT r.report_id, f.report_id AS facts_id,
                   f.net_profit_attr AS np, f.revenue AS rev
            FROM financial_reports r
            LEFT JOIN financial_facts f ON f.report_id = r.report_id
            WHERE r.symbol = ? AND r.period_end = ? AND r.is_cumulative = 1
            ORDER BY r.revision DESC LIMIT 1
            """,
            (symbol, pe)).fetchone()
        if header is not None and (header["np"] is not None
                                   or header["rev"] is not None):
            # 已有 income facts：对账不覆盖
            for got, want, fname in ((np_attr, header["np"], "归母净利润"),
                                     (revenue, header["rev"], "营业总收入")):
                d = _rel_diff(got, want)
                if d is not None and d > _INCOME_TOLERANCE:
                    mismatches.append(f"{symbol} {pe} {fname}: 摘要 {got} vs 库 {want}")
            continue
        if header is None:
            report_id, created = _find_or_create_report_header(
                conn, symbol=symbol, period_end=pe, currency="CNY",
                raw_object_id=raw_object_id, run_id=run_id, now=now)
            created_headers += created
        else:
            report_id = header["report_id"]
        if header is not None and header["facts_id"] is not None:
            # 事实行存在但关键字段全空：只补 NULL 列，不动已有值
            conn.execute(
                "UPDATE financial_facts SET revenue = COALESCE(revenue, ?),"
                " net_profit_attr = COALESCE(net_profit_attr, ?),"
                " eps_basic = COALESCE(eps_basic, ?), updated_at = ?"
                " WHERE report_id = ?",
                (revenue, np_attr, eps, now, report_id))
        else:
            conn.execute(
                "INSERT INTO financial_facts (report_id, revenue, net_profit_attr,"
                " eps_basic, eps_diluted, shares_issued_end, shares_float_end,"
                " share_count_type, updated_at)"
                " VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                (report_id, revenue, np_attr, eps, now))
        backfilled_facts += 1
    if backfilled_facts:
        result.incomplete_reasons.append(
            f"THS 摘要回填 {backfilled_facts} 期 income facts"
            f"（历史期次，available_at=入库时间降级，仅服务趋势分析）")
    if mismatches:
        result.incomplete_reasons.append(
            f"income 对账超差 {len(mismatches)} 处（容差 0.5%）: "
            + "; ".join(mismatches[:5]))
    return result


def parse_holder_stats_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                           result: IngestResult) -> IngestResult:
    """akshare 股东户数 CSV → holder_stats（migration 0009，筹码集中度间接指标）。

    列：code,setcode,stat_date,holder_count,holder_count_prev,holder_count_delta,
    holder_count_delta_pct,avg_hold_value,avg_hold_shares,total_share,share_change,
    announced_at（采集器已把增减比例归一为小数）。symbol 从文件名
    `{symbol}_gdhs.csv` 取。upsert 语义：同 (symbol, stat_date) 内容一致幂等跳过、
    变化原地更新+ingested_at 刷新（快照风格，无 revision 链）。
    """
    m = re.match(r"(\d{6}\.(?:SH|SZ|BJ))_gdhs", Path(path).stem)
    if m is None:
        result.conflicts += 1
        result.errors.append(f"文件名不符合 {{symbol}}_gdhs.csv 约定: {path.name}")
        return result
    symbol = m.group(1)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.conflicts += 1
        result.errors.append("股东户数 CSV 无数据行")
        return result
    now = utc_now()
    run_id = Path(path).parent.name
    for rec in rows:
        stat = (rec.get("stat_date") or "").strip()
        if not _PERIOD_RE.match(stat):
            result.skipped += 1
            continue
        try:
            holder = int(float(rec["holder_count"]))
        except (KeyError, TypeError, ValueError):
            result.skipped += 1
            result.notes.append(f"{stat} holder_count 缺失/非法，行级跳过")
            continue
        vals = {
            "holder_count": holder,
            "holder_count_prev": int(float(rec["holder_count_prev"]))
            if (rec.get("holder_count_prev") or "").strip() else None,
            "holder_count_delta": int(float(rec["holder_count_delta"]))
            if (rec.get("holder_count_delta") or "").strip() else None,
            "holder_count_delta_pct": dec_str(rec.get("holder_count_delta_pct")),
            "avg_hold_value": dec_str(rec.get("avg_hold_value")),
            "avg_hold_shares": dec_str(rec.get("avg_hold_shares")),
            "total_share": dec_str(rec.get("total_share")),
            "share_change": dec_str(rec.get("share_change")),
            "announced_at": (rec.get("announced_at") or "").strip() or None,
        }
        old = conn.execute(
            "SELECT * FROM holder_stats WHERE symbol = ? AND stat_date = ?",
            (symbol, stat)).fetchone()
        if old is None:
            conn.execute(
                """
                INSERT INTO holder_stats (symbol, stat_date, holder_count,
                    holder_count_prev, holder_count_delta, holder_count_delta_pct,
                    avg_hold_value, avg_hold_shares, total_share, share_change,
                    announced_at, source, raw_object_id, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, stat, vals["holder_count"], vals["holder_count_prev"],
                 vals["holder_count_delta"], vals["holder_count_delta_pct"],
                 vals["avg_hold_value"], vals["avg_hold_shares"],
                 vals["total_share"], vals["share_change"], vals["announced_at"],
                 SOURCE, raw_object_id, now))
            result.inserted += 1
            continue

        # 库列 REAL affinity 返回 float，比较前把新值归一为 float（None 透传）
        new_vals = tuple(
            vals[c] if c == "announced_at"
            else (int(vals[c]) if c in ("holder_count", "holder_count_prev",
                                        "holder_count_delta")
                  and vals[c] is not None
                  else (float(vals[c]) if vals[c] is not None else None))
            for c in ("holder_count", "holder_count_prev", "holder_count_delta",
                      "holder_count_delta_pct", "avg_hold_value", "avg_hold_shares",
                      "total_share", "share_change", "announced_at"))
        old_vals = tuple(old[c] for c in (
            "holder_count", "holder_count_prev", "holder_count_delta",
            "holder_count_delta_pct", "avg_hold_value", "avg_hold_shares",
            "total_share", "share_change", "announced_at"))
        if all(vals_equal(a, b) for a, b in zip(old_vals, new_vals)):
            result.skipped += 1
            continue
        conn.execute(
            """
            UPDATE holder_stats SET holder_count=?, holder_count_prev=?,
                holder_count_delta=?, holder_count_delta_pct=?, avg_hold_value=?,
                avg_hold_shares=?, total_share=?, share_change=?, announced_at=?,
                raw_object_id=?, ingested_at=?
            WHERE symbol=? AND stat_date=?
            """,
            (*new_vals, raw_object_id, now, symbol, stat))
        result.updated += 1
    if result.inserted or result.updated:
        result.notes.append(
            f"{symbol} 股东户数 inserted={result.inserted} updated={result.updated}"
            f"（run={run_id}）")
    return result


def parse_forecast_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                       result: IngestResult) -> IngestResult:
    """akshare 一致预期 CSV → forecasts（列对齐 kimi 约定，转发 sfd 已验证解析）。

    采集器（collect_forecast）把同花顺盈利预测换算/映射为 kimi forecast 列约定
    （ths_fore_np_fyN_stock，单位元），payload_json 全量保留附加列
    （ak_np_orgs/ak_np_min/ak_np_max/ak_eps 等）。source 记为 akshare，
    与 kimi 源快照并存，card_inputs 取最新快照（§3.7）。
    """
    return _sfd_parse_forecast(conn, path, raw_object_id, result, source=SOURCE)


def parse_stock_info_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                         result: IngestResult) -> IngestResult:
    """akshare 股本快照 CSV → share_capital_events（snapshot_group_total/group_total）。

    列对齐 kimi stock_info 约定（thscode + ths_total_shares_stock 集团总股本）。
    与 kimi 源**可切换**：同一 symbol 同一 effective_at 已存在其他来源的
    group_total 快照时——股本一致则幂等跳过（不重复写，避免 PE 取数歧义），
    股本不一致记 conflict 交人工核对（§3.2 数据冲突）。

    effective_at 推导：该股 daily_bars 最早交易日（覆盖保留区间起点，与既有
    各股快照惯例一致）；无日线数据时报错不猜（§2.5）。
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    rec = next((r for r in rows if (r.get("thscode") or "").strip()), None)
    if rec is None:
        result.conflicts += 1
        result.errors.append("股本快照 CSV 缺少 thscode")
        return result
    symbol = rec["thscode"].strip()
    if not (rec.get("ths_total_shares_stock") or "").strip():
        result.conflicts += 1
        result.errors.append(f"{symbol} 无 ths_total_shares_stock 字段值（§2.5 不猜）")
        return result
    row = conn.execute(
        "SELECT MIN(trade_date) AS d FROM daily_bars WHERE symbol = ?", (symbol,),
    ).fetchone()
    effective_at = row["d"] if row else None
    if not effective_at:
        result.conflicts += 1
        result.errors.append(
            f"{symbol} daily_bars 为空，无法推导股本快照 effective_at（§2.5 不猜）")
        return result

    shares = str(Decimal(rec["ths_total_shares_stock"].strip()).to_integral_value())
    cross = conn.execute(
        """
        SELECT sce_id, source, shares_issued_after FROM share_capital_events
        WHERE symbol = ? AND effective_at = ? AND share_count_type = 'group_total'
        """,
        (symbol, effective_at),
    ).fetchone()
    if cross is not None:
        if Decimal(cross["shares_issued_after"]) == Decimal(shares):
            result.skipped += 1
            result.notes.append(
                f"{symbol} {effective_at} 已有同源股本 {shares} 的 group_total 快照"
                f"（{cross['source']}），幂等跳过")
            return result
        result.conflicts += 1
        result.errors.append(
            f"股本冲突：{symbol} {effective_at} 已有 group_total "
            f"{cross['shares_issued_after']}（{cross['source']}），akshare 快照 {shares}"
            "（§3.2 数据冲突，交人工核对）")
        return result

    run_id = Path(path).parent.name
    try:
        res = valuation.load_group_total_snapshot(
            conn, symbol, path, effective_at=effective_at, run_id=run_id,
            source=SOURCE, api_label=_STOCK_INFO_API,
            raw_prefix="raw_ak_stock_info")
    except ValueError as exc:
        result.conflicts += 1
        result.errors.append(str(exc))
        return result
    if res["inserted"]:
        result.inserted += 1
    else:
        result.skipped += 1
        result.notes.append(f"{symbol} {effective_at} group_total 快照已存在，幂等跳过")
    return result


def parse_index_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """akshare 指数日线 CSV → index_bars（列同行情 CSV）。"""
    bars: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for rec in csv.DictReader(f):
            code = (rec.get("thscode") or "").strip()
            raw_time = (rec.get("time") or "").strip()
            try:
                trade_date = datetime.strptime(raw_time, "%Y%m%d").date().isoformat()
            except ValueError:
                result.conflicts += 1
                result.errors.append(f"time 列无法解析: {raw_time!r}")
                return result
            row = {
                "open": _num(rec.get("open")), "high": _num(rec.get("high")),
                "low": _num(rec.get("low")), "close": _num(rec.get("close")),
                "volume": _num(rec.get("volume")),
            }
            bad = _validate_bar_row(row, trade_date)
            if bad == "EMPTY":
                result.skipped += 1
                continue
            if bad:
                # 指数源质量波动（如新浪恒生部分历史 open=0）：行级跳过，不整批回滚（§2.5）
                result.skipped += 1
                result.notes.append(f"{trade_date} 指数行非法，跳过: {bad}")
                continue
            bars.append({
                "index_code": code, "trade_date": trade_date, **row,
                "currency": (rec.get("currency") or "").strip() or None,
            })
    run_id = Path(path).parent.name
    upsert_index_bars(conn, bars, source=SOURCE, raw_object_id=raw_object_id,
                      run_id=run_id, result=result)
    return result


def _match_watchlist(conn: sqlite3.Connection, text: str) -> list[str]:
    """按 watchlist 名称/别名/六位代码匹配文本中的股票。"""
    hits: set[str] = set()
    for m in _SYMBOL_RE.finditer(text):
        hits.add(m.group(1))
    for row in conn.execute(
        "SELECT symbol, name, aliases_json FROM watchlist WHERE active = 1"
    ):
        tokens = [row["name"]]
        try:
            import json
            tokens.extend(json.loads(row["aliases_json"] or "[]"))
        except json.JSONDecodeError:
            pass
        for tok in tokens:
            if tok and tok in text:
                hits.add(row["symbol"])
    return sorted(hits)


def parse_telegraph_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                        result: IngestResult) -> IngestResult:
    """财联社电报 CSV → events + event_symbols。

    去重：优先 source_external_id（采集器生成 cls_时间戳），其次 content_hash（§3.6）。
    股票关联：标题+内容 中按 watchlist 名称/别名/六位代码匹配。
    available_at：快讯为即时消息，取 published_at（§2.1；公告类保守规则不适用）。
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("电报 CSV 无数据行")
        return result

    now = utc_now()
    watchlist_cache: list[tuple[str, list[str]]] | None = None

    for rec in rows:
        title = (rec.get("title") or "").strip()
        content = (rec.get("content") or "").strip()
        published_at = (rec.get("published_at") or "").strip()
        ext_id = (rec.get("source_external_id") or "").strip()
        content_hash = (rec.get("content_hash") or "").strip()
        if not title or not published_at:
            # 单条残缺（如无标题的图片快讯）：行级跳过，不整批回滚（§2.5）
            result.skipped += 1
            result.notes.append(
                f"电报行缺标题或 published_at（title={title!r}），行级跳过")
            continue

        # 去重：source_external_id 优先，content_hash 兜底
        dup = None
        if ext_id:
            dup = conn.execute(
                "SELECT event_id FROM events WHERE source = ? AND source_external_id = ?",
                (SOURCE, ext_id),
            ).fetchone()
        if dup is None and content_hash:
            dup = conn.execute(
                "SELECT event_id FROM events WHERE source = ? AND content_hash = ?",
                (SOURCE, content_hash),
            ).fetchone()
        if dup is not None:
            result.skipped += 1
            continue

        event_id = "evt_" + hashlib.sha256(
            f"{SOURCE}|{ext_id or content_hash or title}".encode()).hexdigest()[:16]
        summary = (rec.get("summary") or "").strip() or content[:120]
        published_tz = (rec.get("published_tz") or "").strip() or "Asia/Shanghai"

        conn.execute(
            """
            INSERT INTO events (event_id, event_type, event_at, published_at,
                published_tz, available_at, title, summary, canonical_url,
                source, source_external_id, content_hash, raw_object_id, ingested_at,
                source_tier)
            VALUES (?, 'news', NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, published_at, published_tz, published_at,
             title, summary, SOURCE, ext_id or None, content_hash or None,
             raw_object_id, now, announcements.SOURCE_TIER_TELEGRAPH),
        )
        # 股票关联
        text = f"{title} {content}"
        symbols = _match_watchlist(conn, text)
        for sym in symbols:
            conn.execute(
                "INSERT OR IGNORE INTO event_symbols (event_id, symbol) VALUES (?, ?)",
                (event_id, sym),
            )
        result.inserted += 1
    return result


# ---------------------------------------------------------------- 公告 → events/event_symbols（公共引擎薄壳）

def parse_announcement_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                           result: IngestResult) -> IngestResult:
    """akshare cninfo 公告 CSV → events + event_symbols。

    列与标准公告线格式一致（title, time, url, source, summary, code,
    setcode, name），解析/去重/时点口径单一定义在公共引擎
    announcements.parse_disclosure_csv（不在源间互相借用实现）。
    events.source='akshare' 进入 dedup event_id 命名空间，与 tdx
    采到同一公告时互不吞并；同内容重跑幂等跳过。
    """
    return announcements.parse_disclosure_csv(
        conn, path, raw_object_id, result, source=SOURCE)
