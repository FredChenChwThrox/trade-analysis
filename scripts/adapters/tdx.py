"""tongdaxin (tdx-connector) adapter（设计 §3.1-3.2、§3.5-3.7）。

通达信 MCP 数据源，**默认第一优先级**（kimi-datasource 在 tdx 失败时兜底）。

覆盖数据类型：
- kline        A 股/港股/指数日 K 线 CSV → daily_bars / index_bars
                （tdx_kline 返回 Rows，含 amount，弥补 kimi 缺 amount 缺陷）
- announcement 公告 CSV → events + event_symbols（event_type='announcement'）
                （wenda_notice_query 返回，无 uuid，按 title|date 哈希去重）
- quotes       估值/股本快照 CSV → share_capital_events（snapshot_group_total_tdx）
                （tdx_quotes hasCwInfo=1，含 PE/PB/ROE/总市值/股东人数 GDRS）
- financials   财报 CSV → financial_reports + financial_facts（2026-08-23 起新增，
                弥补 kimi 鉴权失效时的财报通道；A 股 ph_agf10_cw_lyb / 港股
                skef10_hk_cwfx。已替代原 SKILL 「已知限制」中提到的 A/H 财务
                parse_financials_csv 缺口）

CSV 列约定（skills/tdx-collect SKILL.md 落盘格式）：
- kline:     code, setcode, data, open, high, low, close, volume, amount,
              name, period, tqflag, unit
- index:     code, setcode, data, open, high, low, close, volume, amount, name, unit
- announcement: title, time, url, source, summary, code, setcode, name
- quotes:    code, setcode, name, snapshot_at, hqdate, hqtime, now, close,
              pe, pb, mgsy, mgjzc, zsz, zgb, ltgb, gdrs, ipoprice,
              zzc, jzc, jly, yysr, jyxjl
- financials: code, setcode, period_end, fiscal_year, revenue, net_profit_attr,
              eps_basic, eps_diluted, currency, unit, is_cumulative, published_at
              （A 股 tdx_api_data 利润表 ph_agf10_cw_lyb / 港股 skef10_hk_cwfx fixedTag=1；
              每期一行；与 stock_finance_data 同命名约定 `{symbol}_is_{period}.csv`）

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
from decimal import Decimal, InvalidOperation
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


# ---------------------------------------------------------------- 财报 → financial_reports/facts

# tdx 财务数据通过 tdx_api_data 工具获取，金额单位不一致（A 股「元」、港股「万元」）。
# 统一转「元」入库。period_type 按 MM-DD 推断（与 stock_finance_data 一致）。
_FIN_PERIOD_TYPE = {"1231": "annual", "0630": "interim", "0331": "quarterly", "0930": "quarterly"}

# tdx 港股财务接口返回的「币种」字段（agent 探查 2026-08-23 确认）：00700.HK
# 该字段显示"人民币"而非港元；系统按 CNY 入库，由 valuation.py 自行处理汇率。

# 报表口径单位。tdx A 股 ph_agf10_cw_lyb 返回「元」原始，港股 skef10_hk_cwfx 返回「万元」。
# CSV 的 `unit` 列可显式标 raw_unit；adapter 按 unit 折算。系数用 int 保证 Decimal 精确。
_UNIT_TO_YUAN = {
    "yuan": 1,        # A 股原始
    "万元": 10_000,
    "wan_yuan": 10_000,
    "百万元": 1_000_000,
    "million_yuan": 1_000_000,
    "亿元": 100_000_000,
    "hundred_million_yuan": 100_000_000,
}


def _fin_unit_to_yuan(unit_s: str | None) -> int | None:
    """CSV unit 文本 → 元换算系数（int，Decimal 精确）。未知单位返 None（按不猜处理，让调用方记录 incomplete）。"""
    if unit_s is None:
        return None
    s = str(unit_s).strip()
    return _UNIT_TO_YUAN.get(s)


def _fin_period_from_filename(path: Path) -> str | None:
    """文件名 `{symbol}_is_{YYYYMMDD}.csv` → YYYY-MM-DD（与 sfd._period_from_filename 复用规则）。"""
    m = re.search(r"_is_(\d{8})", path.name)
    if not m:
        return None
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def parse_financials_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                         result: IngestResult) -> IngestResult:
    """tdx_api_data 财报 CSV → financial_reports + financial_facts（2026-08-23 新增）。

    raw CSV 列：code,setcode,period_end,fiscal_year,revenue,net_profit_attr,
              eps_basic,eps_diluted,currency,unit,is_cumulative,published_at
    报告期取文件名 `{symbol}_is_{YYYYMMDD}.csv`；文件名无 _is_ 时回退 CSV
    period_end 列（YYYY-MM-DD 校验），两者皆无 → conflict 不入库。

    支持 entry：
    - A 股：TdxShareCW.ph_agf10_cw_lyb（fixedTag=00101 年报 / 00102 单季）
    - 港股：TdxSharePCCW.skef10_hk_cwfx（fixedTag=1 损益）

    单位处理：A 股原始「元」/ 港股「万元」→ 统一转「元」入库（§9.5 关键决策值定点）。
    period_type 从 period_end MM-DD 推断；is_cumulative 默认 1（季报/中报累计）。
    published_at：A 股原始接口不返回，留 NULL（pit_backfill 用 wenda_notice_query 回填）；
              港股 entry 直接返回「公告日期」，可填入。
    修订语义：相同报告期内容变化 → 新增 revision 行，不覆盖（§3.7 硬门槛，与 sfd 一致）。
    股本字段（shares_issued_end/float_end）：tdx 财报接口不返回，本批不入库；
              后续走 tdxf10_gg_gbjg 股本结构另行采集。
    """
    period_end = _fin_period_from_filename(path)

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.conflicts += 1
        result.errors.append("财报 CSV 无数据行")
        return result
    rec = rows[0]
    if period_end is None:
        # 文件名无 _is_YYYYMMDD：回退 CSV period_end 列（YYYY-MM-DD）
        pe_raw = (rec.get("period_end") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pe_raw):
            period_end = pe_raw
            result.notes.append(
                f"文件名无 _is_ 报告期，按 CSV period_end 列取值: {period_end}")
        else:
            result.conflicts += 1
            result.errors.append(
                f"文件名不含 _is_YYYYMMDD 且 CSV period_end 列缺失/非法: {path.name}")
            return result
    if len(rows) > 1:
        result.notes.append(f"财报 CSV 含 {len(rows)} 行，仅取首行入库（按单期约定）")

    code = (rec.get("code") or "").strip()
    setcode = (rec.get("setcode") or "").strip()
    if not code or not setcode:
        result.conflicts += 1
        result.errors.append(f"缺 code/setcode 列或值为空: {path.name}")
        return result
    try:
        symbol = _symbol_from_code_setcode(code, setcode)
    except ValueError as e:
        result.conflicts += 1
        result.errors.append(str(e))
        return result

    mmdd = period_end[5:7] + period_end[8:10]
    period_type = _FIN_PERIOD_TYPE.get(mmdd, "quarterly")
    fiscal_year = int(period_end[:4])
    is_cumulative_raw = (rec.get("is_cumulative") or "1").strip()
    is_cumulative = 1 if is_cumulative_raw in ("1", "true", "True", "yes") else 0

    # currency：A 股默认 CNY；港股 entry 的「币种」字段若指明则按指明值入库（agent 实测显示「人民币」）
    market = market_of(symbol)
    currency = (rec.get("currency") or "").strip()
    if not currency:
        currency = "CNY"
    if market == "HK":
        # 港股报表在 tdx 体系下都按人民币计价（agent 2026-08-23 实测 00700 列「币种=人民币」），
        # 但若数据本身标了 HKD/USD 也按原值入库，由 valuation.py 自行决定汇率换算
        pass

    # 单位换算
    unit_s = (rec.get("unit") or "").strip() or None
    factor = _fin_unit_to_yuan(unit_s)
    if factor is None:
        result.incomplete_reasons.append(
            f"{symbol} {period_end} unit 字段不可识别 ({unit_s!r})，按 yuan=1 兜底（降级）")
        factor = 1

    def _yuan(raw: str | None) -> str | None:
        v = dec_str(raw)
        if v is None:
            return None
        try:
            d = Decimal(v) * Decimal(factor)
        except (InvalidOperation, ValueError):
            return None
        s = format(d, "f")
        # 去掉整数末尾的 ".0+0"（_yuan 用于金额字段，EPS 走 dec_str 不经过本函数）
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s

    facts = {
        "revenue": _yuan(rec.get("revenue")),
        "net_profit_attr": _yuan(rec.get("net_profit_attr")),
        "eps_basic": dec_str(rec.get("eps_basic")),    # EPS 已是元小数，不换算
        "eps_diluted": dec_str(rec.get("eps_diluted")),
    }

    # 披露日（仅港股接口原样返回）；A 股留 NULL 让 pit_backfill 回填
    published_at_raw = (rec.get("published_at") or "").strip()
    published_at_utc: str | None = None
    published_tz: str | None = None
    if published_at_raw:
        try:
            tz = market_tz(market)
            published_at_utc = datetime.fromisoformat(published_at_raw).replace(
                tzinfo=tz).astimezone(timezone.utc).isoformat()
            published_tz = str(tz)
        except ValueError:
            result.incomplete_reasons.append(
                f"{symbol} {period_end} published_at 解析失败 ({published_at_raw!r})，降级 NULL")

    # 修订升级：内容一致跳过；不一致新增 revision
    existing = conn.execute(
        """
        SELECT r.report_id, r.revision, r.published_at, r.available_at,
               f.revenue, f.net_profit_attr,
               f.eps_basic, f.eps_diluted
        FROM financial_reports r
        JOIN financial_facts f ON f.report_id = r.report_id
        WHERE r.symbol = ? AND r.period_end = ? AND r.period_type = ?
          AND r.is_cumulative = ?
        ORDER BY r.revision DESC
        """,
        (symbol, period_end, period_type, is_cumulative),
    ).fetchall()

    if existing:
        latest = existing[0]
        same = all(latest[k] == facts[k] for k in facts)
        if same:
            # 披露日补齐：已有报告 published_at 为 NULL（D1.3 降级入库）而本批 CSV
            # 带来披露日（如 akshare NOTICE_DATE）时，回填 published_at/available_at
            # 并记 data_revisions；事实数字未变，不新增 revision
            if published_at_utc is not None and latest["published_at"] is None:
                avail = _next_open_available_at(
                    load_calendar(conn, market), published_at_raw, market)
                src_row = conn.execute(
                    "SELECT source FROM raw_objects WHERE raw_object_id = ?",
                    (raw_object_id,)).fetchone()
                src = src_row["source"] if src_row else SOURCE
                record_revision(
                    conn, table_name="financial_reports",
                    record_key={"report_id": latest["report_id"], "symbol": symbol,
                                "period_end": period_end, "period_type": period_type},
                    old_value={"published_at": None,
                               "available_at": latest["available_at"]},
                    new_value={"published_at": published_at_utc,
                               "published_tz": published_tz,
                               "available_at": avail,
                               "source_file": Path(path).name,
                               "raw_object_id": raw_object_id},
                    source=src,
                    reason="披露日回填：published_at 由 NULL 补为本批披露日（解除 D1.3 降级）",
                    run_id=Path(path).parent.name)
                conn.execute(
                    """
                    UPDATE financial_reports
                    SET published_at = ?, published_tz = ?, available_at = ?
                    WHERE report_id = ?
                    """,
                    (published_at_utc, published_tz, avail, latest["report_id"]))
                result.updated += 1
                result.notes.append(
                    f"{symbol} {period_end} 内容一致，回填 published_at={published_at_raw}")
            else:
                result.skipped += 1
                result.notes.append(
                    f"{symbol} {period_end} 报告内容一致，跳过（已有 revision={latest['revision']}）")
            return result
        revision = latest["revision"] + 1
        result.notes.append(
            f"{symbol} {period_end} 检测到更正，新增 revision={revision}")
    else:
        revision = 1

    now = utc_now()
    if published_at_utc is None:
        # A 股 entry 不返回 published_at，§2.1 降级：available_at 取入库时间，
        # pe_status 由 valuation.py 后续打 degraded_available_at 标
        result.incomplete_reasons.append(
            f"{symbol} {period_end} tdx 财报接口无 published_at，"
            f"available_at 取入库时间（降级，待 pit_backfill）")
        available_at_utc = now
    else:
        available_at_utc = _next_open_available_at(
            load_calendar(conn, market), published_at_raw, market)

    cur = conn.execute(
        """
        INSERT INTO financial_reports (symbol, period_end, period_type, fiscal_year,
            published_at, published_tz, available_at, revision,
            currency, unit, is_cumulative, raw_object_id, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, period_end, period_type, fiscal_year,
         published_at_utc, published_tz, available_at_utc, revision,
         currency, "yuan", is_cumulative, raw_object_id, now),
    )
    report_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO financial_facts (report_id, revenue, net_profit_attr,
            eps_basic, eps_diluted, shares_issued_end, shares_float_end,
            share_count_type, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
        """,
        (report_id, facts["revenue"], facts["net_profit_attr"],
         facts["eps_basic"], facts["eps_diluted"], now),
    )
    record_revision(conn, table_name="financial_reports", record_key={
        "symbol": symbol, "period_end": period_end, "period_type": period_type,
        "is_cumulative": is_cumulative, "revision": revision},
        old_value=None, new_value=None, source=SOURCE,
        reason=f"tdx 财报入库 revision={revision}",
        run_id=Path(path).parent.name)  # 采集批次目录名（与 yahoo_finance 约定一致）
    result.inserted += 1
    return result
