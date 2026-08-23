"""tongdaxin (tdx-connector) adapter（设计 §3.1-3.2、§3.5-3.7）。

通达信 MCP 数据源，**默认第一优先级**（kimi-datasource 在 tdx 失败时兜底）。

覆盖数据类型：
- kline        A 股/港股/指数日 K 线 CSV → daily_bars / index_bars
                （tdx_kline 返回 Rows，含 amount，弥补 kimi 缺 amount 缺陷）
- announcement 公告 CSV → events + event_symbols（event_type='announcement'）
                （wenda_notice_query 返回，无 uuid，按 title|date 哈希去重）
- quotes       估值/股本快照 CSV → share_capital_events（snapshot_group_total_tdx）
                （tdx_quotes hasCwInfo=1，含 PE/PB/ROE/总市值/股东人数 GDRS）

CSV 列约定（skills/tdx-collect SKILL.md 落盘格式）：
- kline:     code, setcode, data, open, high, low, close, volume, amount,
              name, period, tqflag, unit
- index:     code, setcode, data, open, high, low, close, volume, amount, name, unit
- announcement: title, time, url, source, summary, code, setcode, name
- quotes:    code, setcode, name, snapshot_at, hqdate, hqtime, now, close,
              pe, pb, mgsy, mgjzc, zsz, zgb, ltgb, gdrs, ipoprice,
              zzc, jzc, jly, yysr, jyxjl

setcode → symbol 后缀（与 SUFFIX_MARKET 对齐）：
  1=SH(沪A) 0=SZ(深A) 2=BJ(北交所) 31=HK(港股) 62=SH(中证指数,系统内 .SH)

复权口径（§3.3）：daily_bars 存不复权价 + price_adj_factor。
tqflag=0（不复权）入 daily_bars，因子由 upsert_daily_bars 继承上一交易日；
tqflag=1/2（前/后复权）文件不入 daily_bars（留给 D1.5 复权模块，与 kimi
*_forward 约定一致，ingest CLI 路由层跳过）。

单位换算（关键，2026-08-21 实测确认）：
- tdx Rows.Volume 单位是"手"（Unit=100），系统 volume_raw 口径是"股"（与 kimi 一致）。
  adapter 按 CSV 的 unit 列换算：volume_raw = volume × unit（unit=100 时手→股，
  unit=1 时原值，如指数 Unit=1）。
- tdx Rows.Amount 单位是"元"（成交额），与系统 amount_raw 口径一致，不换算。
  kimi 缺 amount（NULL），tdx 是优势字段。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.adapters.common import (
    IngestResult,
    dec_str,
    load_calendar,
    market_of,
    market_tz,
    record_revision,
    utc_now,
)
from scripts.adapters.stock_finance_data import (
    _validate_bar_row,
    upsert_daily_bars,
    upsert_index_bars,
)

SOURCE = "tdx"

# setcode → symbol 后缀（个股）/ index 后缀（指数）
# 与 common.SUFFIX_MARKET 对齐；62 中证指数系统内统一 .SH（与 yahoo INDEX_CODE_ALIAS 同口径）
SETCODE_SUFFIX = {
    "1": "SH",    # 沪市 A 股
    "0": "SZ",    # 深市 A 股
    "2": "BJ",    # 北交所
    "31": "HK",   # 港股
    "62": "SH",   # 中证指数（000300 沪深300）
    "32": "HK",   # 港股指数
}
INDEX_SETCODES = {"62", "32"}  # 走 index_bars 而非 daily_bars


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "NA", "nan", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _vol_to_shares(vol_raw, unit_raw) -> float | None:
    """tdx Rows.Volume 按单位换算为系统 volume_raw 口径（股）。

    tdx Unit=100（手）→ ×100 转股；Unit=1（指数，已是股）→ 原值。
    unit 缺失默认 100（A 股/港股个股约定）。2026-08-21 实测：
    603605.SH 08-17 tdx Vol=50372.18 手 × 100 = 5037218 股 ≈ kimi 5037200 股。
    """
    vol = _num(vol_raw)
    if vol is None:
        return None
    unit = _num(unit_raw)
    if unit is None:
        unit = 100.0  # 默认手
    if unit == 1:
        return vol
    return vol * unit


def _symbol_from_code_setcode(code: str, setcode: str) -> str:
    """code + setcode → 系统 symbol（带后缀，如 603605.SH / 00700.HK）。"""
    suffix = SETCODE_SUFFIX.get(str(setcode))
    if suffix is None:
        raise ValueError(f"未知 setcode={setcode}，无法推断 symbol 后缀")
    return f"{code}.{suffix}"


def _tdx_date_to_iso(s: str) -> str:
    """20260821 → 2026-08-21。"""
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    # 兼容已带 - 的格式
    return datetime.fromisoformat(s).date().isoformat()


# ---------------------------------------------------------------- K线 → daily_bars/index_bars

def parse_kline_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """tdx_kline 日 K 线 CSV → daily_bars（A 股/港股，不含指数）。

    列：code, setcode, data, open, high, low, close, volume, amount,
        name, period, tqflag

    tqflag=0（不复权）入 daily_bars；tqflag=1/2 跳过（留给复权模块）。
    """
    calendar_cache: dict[str, dict] = {}
    bars: list[dict] = []
    rows_iter = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
    if not rows_iter:
        result.skipped += 1
        result.notes.append("K 线 CSV 无数据行")
        return result

    for rec in rows_iter:
        setcode = (rec.get("setcode") or "").strip()
        if setcode in INDEX_SETCODES:
            # 指数文件误入 kline 目录：转走 index 路径，但本函数仅处理个股
            result.conflicts += 1
            result.errors.append(
                f"setcode={setcode} 是指数，应入 index 目录而非 kline: {path.name}")
            return result
        code = (rec.get("code") or "").strip()
        if not code or not setcode:
            result.conflicts += 1
            result.errors.append(f"缺 code 或 setcode 列: {path.name}")
            return result
        symbol = _symbol_from_code_setcode(code, setcode)
        market = market_of(symbol)
        tqflag = (rec.get("tqflag") or "0").strip()
        if tqflag in ("1", "2"):
            # 前/后复权文件不入 daily_bars（与 kimi *_forward 同口径，ingest CLI 路由层
            # 也会跳过，本处再防御一次）
            result.skipped += 1
            result.notes.append(
                f"{symbol} tqflag={tqflag}（前/后复权）不入 daily_bars，留给复权模块")
            continue
        try:
            trade_date = _tdx_date_to_iso(rec.get("data") or "")
        except ValueError:
            result.conflicts += 1
            result.errors.append(f"data 列无法解析: {rec.get('data')!r}")
            return result
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
            "volume": _vol_to_shares(rec.get("volume"), rec.get("unit")),
            "amount": _num(rec.get("amount")),
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
        # 通达信 A 股 currency=CNY，港股=HKD
        currency = "HKD" if market == "HK" else "CNY"
        bars.append({
            "symbol": symbol, "trade_date": trade_date, "market": market,
            **row, "currency": currency,
        })
    run_id = Path(path).parent.name
    upsert_daily_bars(conn, bars, source=SOURCE, raw_object_id=raw_object_id,
                      run_id=run_id, result=result)
    return result


def parse_index_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """tdx_kline 指数 CSV → index_bars（setcode=62 中证指数 / 32 港股指数）。

    列同 kline；index_code 经 SETCODE_SUFFIX 后缀归一（000300 → 000300.SH）。
    """
    bars: list[dict] = []
    rows_iter = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
    if not rows_iter:
        result.skipped += 1
        result.notes.append("指数 CSV 无数据行")
        return result

    for rec in rows_iter:
        setcode = (rec.get("setcode") or "").strip()
        code = (rec.get("code") or "").strip()
        if not code or not setcode:
            result.conflicts += 1
            result.errors.append(f"缺 code 或 setcode 列: {path.name}")
            return result
        if setcode not in INDEX_SETCODES:
            result.conflicts += 1
            result.errors.append(
                f"setcode={setcode} 不是指数（应∈{INDEX_SETCODES}）却入 index 目录: {path.name}")
            return result
        index_code = _symbol_from_code_setcode(code, setcode)
        try:
            trade_date = _tdx_date_to_iso(rec.get("data") or "")
        except ValueError:
            result.conflicts += 1
            result.errors.append(f"data 列无法解析: {rec.get('data')!r}")
            return result
        row = {
            "open": _num(rec.get("open")), "high": _num(rec.get("high")),
            "low": _num(rec.get("low")), "close": _num(rec.get("close")),
            "volume": _vol_to_shares(rec.get("volume"), rec.get("unit")),
        }
        bad = _validate_bar_row(row, trade_date)
        if bad == "EMPTY":
            result.skipped += 1
            result.notes.append(f"{trade_date} OHLC 全缺失（残缺 bar），行级跳过")
            continue
        if bad:
            result.conflicts += 1
            result.errors.append(f"{index_code} {bad}")
            return result
        currency = "HKD" if setcode == "32" else "CNY"
        bars.append({
            "index_code": index_code, "trade_date": trade_date, **row,
            "currency": currency,
        })
    upsert_index_bars(conn, bars, source=SOURCE, raw_object_id=raw_object_id,
                      run_id=Path(path).parent.name, result=result)
    return result


# ---------------------------------------------------------------- 公告 → events/event_symbols

_STEM_TICKER = re.compile(r"(\d{6})\.(SH|SZ|BJ)|(\d{4,5})\.HK")


def _ticker_from_stem(path: Path) -> str | None:
    """从文件名 stem 推断 ticker（形如 603605.SH_p1 / 00700.HK_p1）。

    港股 code 为 4-5 位（00700），A 股为 6 位。
    """
    m = _STEM_TICKER.search(path.stem)
    if m:
        if m.group(1):  # A 股 6 位
            return f"{m.group(1)}.{m.group(2)}"
        if m.group(3):  # 港股 4-5 位
            return f"{m.group(3)}.HK"
    return None


def _next_open_available_at(calendar: dict, pub_date: str, market: str) -> str:
    """下一个开市交易日 00:00（本地）→ UTC ISO（§2.1 保守时点）。"""
    tz = market_tz(market)
    d = datetime.fromisoformat(pub_date).date() + timedelta(days=1)
    if calendar:
        while d.isoformat() not in calendar or not calendar[d.isoformat()]["is_open"]:
            d += timedelta(days=1)
            if (d - datetime.fromisoformat(pub_date).date()).days > 40:
                break
    return datetime(d.year, d.month, d.day,
                    tzinfo=tz).astimezone(timezone.utc).isoformat()


def parse_announcement_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                           result: IngestResult) -> IngestResult:
    """tdx wenda_notice_query 公告 CSV → events + event_symbols。

    列：title, time, url, source, summary, code, setcode, name

    去重：通达信公告无 uuid，按 title|pub_date 哈希（§3.6）。
    symbol 关联：优先 CSV 中 code+setcode 推断，回退文件名 stem（含 ticker）。
    available_at：发布日 + 1 开市交易日 00:00 本地（§2.1）。
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("公告 CSV 无数据行")
        return result

    symbol = _ticker_from_stem(path)
    if symbol is None:
        # 回退：CSV 行内的 code+setcode 推断
        first = rows[0]
        code = (first.get("code") or "").strip()
        setcode = (first.get("setcode") or "").strip()
        if code and setcode:
            try:
                symbol = _symbol_from_code_setcode(code, setcode)
            except ValueError:
                pass
    if symbol is None:
        result.conflicts += 1
        result.errors.append(
            f"无法推断 ticker（文件名 stem 不含 ticker，CSV 行也无 code/setcode）: {path.name}")
        return result

    market = market_of(symbol)
    calendar = load_calendar(conn, market)
    if not calendar:
        result.incomplete_reasons.append(
            f"trading_calendar 缺失（market={market}），available_at 取发布日+1 天（降级）")

    now = utc_now()
    for rec in rows:
        title = (rec.get("title") or "").strip()
        time_s = (rec.get("time") or "").strip()
        if not title or not time_s:
            result.conflicts += 1
            result.errors.append(
                f"公告行缺标题或时间列（实得列: {sorted(rec.keys())}）")
            return result
        # time 形如 "2026-08-04 00:00:00"，取前 10 位日期
        pub_date = time_s[:10]
        url = (rec.get("url") or "").strip() or None
        source_tag = (rec.get("source") or "").strip() or None  # "上交所" 等
        summary = (rec.get("summary") or "").strip() or source_tag
        # 去重：title|pub_date 哈希（通达信无 uuid）
        dedup_key = f"{title}|{pub_date}"
        event_id = "evt_" + hashlib.sha256(
            f"{SOURCE}|{dedup_key}".encode()).hexdigest()[:16]

        if conn.execute("SELECT 1 FROM events WHERE event_id = ?",
                        (event_id,)).fetchone():
            result.skipped += 1
            continue

        tz = market_tz(market)
        published_at = datetime.fromisoformat(pub_date).replace(
            tzinfo=tz).astimezone(timezone.utc).isoformat()
        available_at = _next_open_available_at(calendar, pub_date, market)

        conn.execute(
            """
            INSERT INTO events (event_id, event_type, event_at, published_at,
                published_tz, available_at, title, summary, canonical_url,
                source, source_external_id, content_hash, raw_object_id, ingested_at)
            VALUES (?, 'announcement', NULL, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (event_id, published_at, str(tz), available_at, title, summary, url,
             SOURCE, hashlib.sha256(dedup_key.encode()).hexdigest(),
             raw_object_id, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_symbols (event_id, symbol) VALUES (?, ?)",
            (event_id, symbol),
        )
        result.inserted += 1
    return result


# ---------------------------------------------------------------- 估值/股本快照 → share_capital_events

def parse_quotes_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                     result: IngestResult) -> IngestResult:
    """tdx_quotes hasCwInfo=1 估值/股本快照 CSV → share_capital_events。

    列：code, setcode, name, snapshot_at, hqdate, hqtime, now, close,
        pe, pb, mgsy, mgjzc, zsz, zgb, ltgb, gdrs, ipoprice,
        zzc, jzc, jly, yysr, jyxjl

    写 share_capital_events（event_type=snapshot_group_total_tdx，
    share_count_type='group_total_tdx'）单点快照——与 stock_finance_data 的
    'group_total' 快照区分，**不参与 valuation.py 的 PE 取数**（仅认 issued/group_total，
    详见 indicators/valuation.py），作为 tdx 估值/股本/GDRS 备查快照。
    details_json 含 pe/pb/mgsy/mgjzc/zsz/ltgb/gdrs/financials 等。
    zgb（总股本，单位：万股）按系统口径换算为股。
    幂等：(symbol, effective_at, event_type=snapshot_group_total_tdx) 冲突即跳过。
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("估值快照 CSV 无数据行")
        return result
    rec = rows[0]
    code = (rec.get("code") or "").strip()
    setcode = (rec.get("setcode") or "").strip()
    if not code or not setcode:
        result.conflicts += 1
        result.errors.append(f"缺 code 或 setcode 列: {path.name}")
        return result
    symbol = _symbol_from_code_setcode(code, setcode)
    snapshot_at = (rec.get("snapshot_at") or "").strip()
    if not snapshot_at:
        # 回退用 hqdate
        hqdate = (rec.get("hqdate") or "").strip()
        snapshot_at = _tdx_date_to_iso(hqdate) if hqdate else utc_now()[:10]

    # zgb 单位：万股 → 股；非整数/缺值抛错（§2.5 不猜）
    zgb_wan = _num(rec.get("zgb"))
    if zgb_wan is None or zgb_wan <= 0:
        result.conflicts += 1
        result.errors.append(f"{symbol} zgb（总股本）缺失或非正: {rec.get('zgb')!r}")
        return result
    shares_total = int(round(zgb_wan * 10000))  # 万股 → 股

    details = {
        "source": "tdx_quotes_hasCwInfo",
        "snapshot_at": snapshot_at,
        "now": _num(rec.get("now")),
        "close": _num(rec.get("close")),
        "pe": _num(rec.get("pe")),
        "pb": _num(rec.get("pb")),
        "mgsy": _num(rec.get("mgsy")),
        "mgjzc": _num(rec.get("mgjzc")),
        "zsz": _num(rec.get("zsz")),
        "ltgb_wan": _num(rec.get("ltgb")),
        "gdrs": _num(rec.get("gdrs")),
        "ipoprice": _num(rec.get("ipoprice")),
        "zzc_wan": _num(rec.get("zzc")),
        "jzc_wan": _num(rec.get("jzc")),
        "jly_wan": _num(rec.get("jly")),
        "yysr_wan": _num(rec.get("yysr")),
        "jyxjl_wan": _num(rec.get("jyxjl")),
        "note": ("tdx CwInfo 单点快照；share_count_type=group_total_tdx，"
                 "不参与 PE 取数（valuation.py 仅认 issued/group_total），"
                 "仅作估值/股东人数备查；A+H 公司实际只含 A 股（同 stock_finance_data 限制）"),
    }

    now = utc_now()
    exists = conn.execute(
        "SELECT 1 FROM share_capital_events WHERE symbol=? AND effective_at=? AND event_type=?",
        (symbol, snapshot_at, "snapshot_group_total_tdx"),
    ).fetchone()
    if exists:
        result.skipped += 1
        result.notes.append(f"{symbol} {snapshot_at} snapshot_group_total_tdx 已存在，跳过")
        return result

    conn.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at, event_type,
            share_change, shares_issued_after, share_count_type, details_json, source,
            raw_object_id, created_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, snapshot_at, now, "snapshot_group_total_tdx",
         str(shares_total), "group_total_tdx",
         json.dumps(details, ensure_ascii=False),
         SOURCE, raw_object_id, now),
    )
    result.inserted += 1
    return result
