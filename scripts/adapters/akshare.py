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

口径（对齐库 schema，§3.2/§9.5）：
- 成交量已在采集器层 ×100 换为「股」；成交额「元」直接入库；
- 财报金额 unit='yuan'，由 tdx 复用逻辑校验；published_at 来自 NOTICE_DATE 披露日。
"""

from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scripts.adapters import announcements
from scripts.adapters.common import (
    IngestResult,
    load_calendar,
    market_of,
    market_tz,
    utc_now,
)
from scripts.adapters.stock_finance_data import (
    _num,
    _validate_bar_row,
    parse_forecast_csv as _sfd_parse_forecast,
    upsert_daily_bars,
    upsert_index_bars,
)
from scripts.adapters.tdx import parse_financials_csv as _tdx_parse_financials
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
    return result


def parse_financials_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                         result: IngestResult) -> IngestResult:
    """akshare 利润表 CSV → financial_reports/facts（复用 tdx 已验证解析）。

    akshare 采集器按 tdx 列约定落盘（code,setcode,period_end,...,published_at），
    其中 published_at = NOTICE_DATE（东财正式披露日），正好补上 A 股披露时间缺口。
    """
    return _tdx_parse_financials(conn, path, raw_object_id, result)


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
