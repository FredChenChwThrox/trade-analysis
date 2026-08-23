"""tianyancha adapter（设计 §3.1-3.2、§3.6）。

覆盖数据类型：
- announcement 公告 CSV → events + event_symbols（event_type='announcement'）

来源接口（实测记录 docs/probe_20260815_tianyancha.md）：tianyancha_api_call，
api_call_name="上市信息-上市公告"，keyword=公司全称，分页 pageNum/pageSize=20，
按公告日倒序。返回列：stock_name, ossUrl, companyName, name, id, time,
announcementType, title, uuid, stock_code。

字段映射：time → 公告日期（YYYY-MM-DD）、title → title、ossUrl → canonical_url、
uuid → source_external_id（天然唯一 ID，去重优先，其次 title|date hash，§3.6）、
announcementType → summary、stock_code → 裸 6 位代码。

symbol 关联：stock_code 只有裸 6 位，完整 ticker 从文件名 stem 取
（形如 603605.SH_p3，正则 \\d{6}\\.(SH|SZ|BJ)），并校验与 stock_code 一致；
A+H 两地上市公司返回会混 H 股公告（如 stock_code=HK.03288）→ 行级跳过记
note；不同 A 股 6 位代码（采错公司）→ conflict 整批回滚。
available_at：发布时间只有日期粒度，取下一个开市交易日 00:00 本地
（§2.1：不假定盘前发布；与 stock_finance_data 公告同款口径，
复用 load_calendar / market_tz）。
"""

from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.adapters.common import (
    IngestResult,
    load_calendar,
    market_tz,
    utc_now,
)

SOURCE = "tianyancha"

_STEM_TICKER = re.compile(r"(\d{6})\.(SH|SZ|BJ)")


def _ticker_from_stem(path: Path) -> str | None:
    m = _STEM_TICKER.search(path.stem)
    return m.group(0) if m else None


def _next_open_available_at(calendar: dict, pub_date: str) -> str:
    """下一个开市交易日 00:00（本地）→ UTC ISO（§2.1 保守时点）。"""
    tz = market_tz("CN")
    d = datetime.fromisoformat(pub_date).date() + timedelta(days=1)
    if calendar:
        while d.isoformat() not in calendar or not calendar[d.isoformat()]["is_open"]:
            d += timedelta(days=1)
            if (d - datetime.fromisoformat(pub_date).date()).days > 40:
                break
    return datetime(d.year, d.month, d.day,
                    tzinfo=tz).astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- 公告 → events/event_symbols

def parse_announcement_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                           result: IngestResult) -> IngestResult:
    """天眼查公告 CSV → events + event_symbols（event_type='announcement'）。

    去重：uuid（source_external_id）优先，其次标题+日期哈希（§3.6）。
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("公告 CSV 无数据行")
        return result

    symbol = _ticker_from_stem(path)
    if symbol is None:
        result.conflicts += 1
        result.errors.append(f"无法从文件名推断 ticker（期望形如 603605.SH_p3）: {path.name}")
        return result
    code6 = symbol.split(".")[0]

    calendar = load_calendar(conn, "CN")
    if not calendar:
        result.incomplete_reasons.append(
            "trading_calendar 缺失（market=CN），available_at 取发布日+1 天（降级）")
    now = utc_now()
    for rec in rows:
        title = (rec.get("title") or "").strip()
        date_s = (rec.get("time") or "").strip()
        if not title or not date_s:
            result.conflicts += 1
            result.errors.append(
                f"公告行缺标题或日期列（实得列: {sorted(rec.keys())}）")
            return result
        stock_code = (rec.get("stock_code") or "").strip()
        if stock_code and stock_code != code6:
            if re.fullmatch(r"\d{6}", stock_code):
                # 不同 A 股 6 位代码：采错公司，整批回滚
                result.conflicts += 1
                result.errors.append(
                    f"stock_code 与文件名 ticker 不一致: {stock_code} vs {symbol}")
                return result
            # A+H 两地上市混入的 H 股/其他市场公告（如 HK.03288）：行级跳过
            result.skipped += 1
            note = f"H股/其他市场公告跳过（stock_code={stock_code}）"
            if note not in result.notes:
                result.notes.append(note)
            continue
        pub_date = date_s[:10]
        external_id = (rec.get("uuid") or "").strip() or None
        dedup_key = external_id or f"{title}|{pub_date}"
        event_id = "evt_" + hashlib.sha256(
            f"{SOURCE}|{dedup_key}".encode()).hexdigest()[:16]

        if conn.execute("SELECT 1 FROM events WHERE event_id = ?",
                        (event_id,)).fetchone():
            result.skipped += 1
            continue

        tz = market_tz("CN")
        published_at = datetime.fromisoformat(pub_date).replace(
            tzinfo=tz).astimezone(timezone.utc).isoformat()
        available_at = _next_open_available_at(calendar, pub_date)

        conn.execute(
            """
            INSERT INTO events (event_id, event_type, event_at, published_at,
                published_tz, available_at, title, summary, canonical_url,
                source, source_external_id, content_hash, raw_object_id, ingested_at)
            VALUES (?, 'announcement', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, published_at, "Asia/Shanghai", available_at, title,
             (rec.get("announcementType") or "").strip() or None,
             (rec.get("ossUrl") or "").strip() or None,
             SOURCE, external_id,
             hashlib.sha256(f"{title}|{pub_date}".encode()).hexdigest(),
             raw_object_id, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_symbols (event_id, symbol) VALUES (?, ?)",
            (event_id, symbol),
        )
        result.inserted += 1
    return result
