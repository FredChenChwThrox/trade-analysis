"""公告 → events/event_symbols 公共解析引擎（源中立，D1.3 抽取）。

设计动机：tdx wenda 与 akshare cninfo 两类采集器落盘共用同一"标准公告线格式"，
解析/去重/时点口径只应存在一份实现；各数据源 adapter 仅做薄壳委托，
来源归属由 events.source + event_id 命名空间隔离（§3.6），血缘不混淆。

标准线格式列（各采集器落盘时对齐）：
    title, time, url, source, summary, code, setcode, name
- title/time 必填，缺任一整文件拒绝（conflict，不部分入库）；
- time 取前 10 位为发布日期（来源当地日）；
- symbol 推断：文件名 stem 的 ticker 优先，回退行内 code+setcode（SETCODE_SUFFIX）；
- 去重：event_id = sha256(f"{source}|{title}|{pub_date}")[:16]，同内容重跑幂等跳过；
- published_at = 发布日 00:00 当地 → UTC；available_at = 发布日 +1 开市交易日
  00:00 当地 → UTC（§2.1）；calendar 缺失降级为 +1 自然日并记 incomplete。

职责边界：本模块只做"标准线格式 → events 写入"。线格式不同的公告源
（sfd 列别名 / tyc uuid 去重）不进本引擎，各自 adapter 内自行处理。
"""

from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scripts.adapters.common import (
    IngestResult,
    load_calendar,
    market_of,
    market_tz,
    next_open_available_at,
    symbol_from_code_setcode,
    utc_now,
)

# 文件名 stem 中 ticker 形如 603605.SH_p1 / 00700.HK_p1（A 股 6 位、港股 4-5 位）
_STEM_TICKER = re.compile(r"(\d{6})\.(SH|SZ|BJ)|(\d{4,5})\.HK")


def _ticker_from_stem(path: Path) -> str | None:
    """从文件名 stem 推断 ticker（形如 603605.SH_p1 / 00700.HK_p1）。"""
    m = _STEM_TICKER.search(path.stem)
    if m:
        if m.group(1):  # A 股 6 位
            return f"{m.group(1)}.{m.group(2)}"
        if m.group(3):  # 港股 4-5 位
            return f"{m.group(3)}.HK"
    return None


def parse_disclosure_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                         result: IngestResult, *, source: str) -> IngestResult:
    """标准公告线格式 CSV → events + event_symbols。

    source：events.source 字段值（'tdx' / 'akshare' 等），同时进入 dedup
    event_id 命名空间——不同源采到同一公告会得到不同 event_id，互不吞并。
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
                symbol = symbol_from_code_setcode(code, setcode)
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
        source_tag = (rec.get("source") or "").strip() or None  # "上交所"/"巨潮资讯" 等
        summary = (rec.get("summary") or "").strip() or source_tag
        # 去重：source 命名空间内 title|pub_date 哈希（线格式无 uuid）
        dedup_key = f"{title}|{pub_date}"
        event_id = "evt_" + hashlib.sha256(
            f"{source}|{dedup_key}".encode()).hexdigest()[:16]

        if conn.execute("SELECT 1 FROM events WHERE event_id = ?",
                        (event_id,)).fetchone():
            result.skipped += 1
            continue

        tz = market_tz(market)
        published_at = datetime.fromisoformat(pub_date).replace(
            tzinfo=tz).astimezone(timezone.utc).isoformat()
        available_at = next_open_available_at(calendar, pub_date, market)

        conn.execute(
            """
            INSERT INTO events (event_id, event_type, event_at, published_at,
                published_tz, available_at, title, summary, canonical_url,
                source, source_external_id, content_hash, raw_object_id, ingested_at)
            VALUES (?, 'announcement', NULL, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (event_id, published_at, str(tz), available_at, title, summary, url,
             source, hashlib.sha256(dedup_key.encode()).hexdigest(),
             raw_object_id, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_symbols (event_id, symbol) VALUES (?, ?)",
            (event_id, symbol),
        )
        result.inserted += 1
    return result
