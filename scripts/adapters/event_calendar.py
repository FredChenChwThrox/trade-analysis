"""event_calendar 采集 CSV → event_calendar 表（消息面 r2 Phase 1，L0 日历层）。

采集侧（scripts/collect/akshare_collect.py --sources calendar，手触发不进 daily）：
    calendar/{date}/{run_id}/report_disclosure.csv  财报披露预约（stock_report_disclosure，
                                                    全市场拉取后仅留 watchlist 行，
                                                    scheduled_date 取"当前预约"=最后一次变更）
    calendar/{date}/{run_id}/unlock.csv             解禁日程（stock_restricted_release_queue_em
                                                    逐股拉取，仅留采集日之后的未来行）

入库侧：ingest 路由 ("akshare", "calendar") → parse_calendar_csv（按文件 stem 分派）。

幂等与命名空间（r2 §3.1）：
    cal_id = "cal_" + sha256(f"{source}|{kind}|{symbol}|{scheduled_date}")[:16]，
    INSERT ON CONFLICT DO NOTHING——同内容重跑幂等跳过；手工种子（config/
    event_calendar.yaml，source='manual'，人工 cal_id）互不冲突。
纪律：本模块只写事实行（kind/symbol/scheduled_date/note），不产生任何研判字段；
    提醒窗口过滤在消费端（scripts/signals/calendar_due.py），不在入库端（r2 §1）。
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

from scripts.adapters.common import IngestResult, utc_now

SOURCE = "akshare"
KIND_DISCLOSURE = "report_disclosure"
KIND_UNLOCK = "unlock"


def _cal_id(kind: str, symbol: str, scheduled_date: str) -> str:
    """确定性 cal_id：同 (源, 类别, 股票, 日期) 重跑必同 id（幂等根基）。"""
    return "cal_" + hashlib.sha256(
        f"{SOURCE}|{kind}|{symbol}|{scheduled_date}".encode()).hexdigest()[:16]


def _insert(conn: sqlite3.Connection, result: IngestResult, *, kind: str,
            symbol: str | None, scheduled_date: str, note: str | None,
            raw_object_id: str) -> None:
    cur = conn.execute(
        """
        INSERT INTO event_calendar (cal_id, kind, symbol, scheduled_date,
                                    source, remind_before_days, note,
                                    raw_object_id, ingested_at)
        VALUES (?, ?, ?, ?, ?, 3, ?, ?, ?)
        ON CONFLICT(cal_id) DO NOTHING
        """,
        (_cal_id(kind, symbol or "", scheduled_date), kind, symbol, scheduled_date,
         SOURCE, note, raw_object_id, utc_now()),
    )
    if cur.rowcount:
        result.inserted += 1
    else:
        result.skipped += 1


def parse_calendar_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                       result: IngestResult) -> IngestResult:
    """calendar 目录 CSV 入口：按文件 stem 分派到披露预约/解禁解析。"""
    stem = path.stem
    if stem.startswith("report_disclosure"):
        return _parse_disclosure(conn, path, raw_object_id, result)
    if stem.startswith("unlock"):
        return _parse_unlock(conn, path, raw_object_id, result)
    result.skipped += 1
    result.notes.append(
        f"event_calendar 未知文件 stem: {path.name}（期望 report_disclosure*/unlock*）")
    return result


def _parse_disclosure(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                      result: IngestResult) -> IngestResult:
    """report_disclosure.csv（symbol,name,period,scheduled_date,first_scheduled,
    actual_disclosed）→ event_calendar(kind=report_disclosure)。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("披露预约 CSV 无数据行")
        return result
    for rec in rows:
        symbol = (rec.get("symbol") or "").strip()
        scheduled = (rec.get("scheduled_date") or "").strip()[:10]
        if not symbol or not scheduled:
            result.skipped += 1
            result.notes.append(f"披露预约行缺 symbol/scheduled_date（{rec}），行级跳过")
            continue
        period = (rec.get("period") or "").strip()
        first = (rec.get("first_scheduled") or "").strip()[:10]
        note = f"财报披露预约（{period}）"
        if first and first != scheduled:
            note += f"，首次预约 {first}（已变更）"
        if (rec.get("actual_disclosed") or "").strip():
            note += "，已实际披露"
        _insert(conn, result, kind=KIND_DISCLOSURE, symbol=symbol,
                scheduled_date=scheduled, note=note, raw_object_id=raw_object_id)
    return result


def _parse_unlock(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                  result: IngestResult) -> IngestResult:
    """unlock.csv（symbol,unlock_date,shares_free,ratio_total,share_type）
    → event_calendar(kind=unlock)，note 携带股数/占比/限售类型事实。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("解禁 CSV 无数据行")
        return result
    for rec in rows:
        symbol = (rec.get("symbol") or "").strip()
        unlock_date = (rec.get("unlock_date") or "").strip()[:10]
        if not symbol or not unlock_date:
            result.skipped += 1
            result.notes.append(f"解禁行缺 symbol/unlock_date（{rec}），行级跳过")
            continue
        shares = (rec.get("shares_free") or "").strip()
        ratio = (rec.get("ratio_total") or "").strip()
        share_type = (rec.get("share_type") or "").strip()
        parts: list[str] = []
        if shares:
            try:
                parts.append(f"解禁 {float(shares) / 1e8:.2f} 亿股")
            except ValueError:
                parts.append(f"解禁 {shares} 股")
        if ratio:
            try:
                parts.append(f"占总市值 {float(ratio):.2%}")
            except ValueError:
                pass
        if share_type:
            parts.append(share_type)
        _insert(conn, result, kind=KIND_UNLOCK, symbol=symbol,
                scheduled_date=unlock_date, note="；".join(parts) or None,
                raw_object_id=raw_object_id)
    return result
