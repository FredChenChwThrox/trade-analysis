"""symbol_industry 采集 CSV → symbol_industry 表（消息面 r2 Phase 3）。

采集：scripts/collect/industry_collect.py（push2delay 全市场，每股最细三级板块），
落盘 industry/{date}/{run_id}/symbol_industry.csv，经 ingest 路由
("akshare","industry") 进入。主键 (symbol, source, classification_date) upsert
（同日重采=事实刷新）；季度刷新产生新 classification_date 行，历史可追溯。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from scripts.adapters.common import IngestResult, utc_now

SOURCE = "akshare_em"


def parse_industry_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                       result: IngestResult) -> IngestResult:
    """symbol_industry.csv（symbol,industry_code,industry_name）→ upsert。

    classification_date 取落盘路径 industry/{date}/ 段（采集日），缺省今天。
    """
    parts = path.parts
    classification_date = (
        parts[parts.index("industry") + 1] if "industry" in parts
        else utc_now()[:10])
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("industry CSV 无数据行")
        return result
    now = utc_now()
    for rec in rows:
        symbol = (rec.get("symbol") or "").strip()
        code = (rec.get("industry_code") or "").strip()
        name = (rec.get("industry_name") or "").strip()
        if not symbol or not code or not name:
            result.skipped += 1
            result.notes.append(f"industry 行缺必填列（{rec}），行级跳过")
            continue
        conn.execute(
            """
            INSERT INTO symbol_industry (symbol, industry_code, industry_name,
                                         source, classification_date,
                                         raw_object_id, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, source, classification_date) DO UPDATE SET
                industry_code=excluded.industry_code,
                industry_name=excluded.industry_name,
                raw_object_id=excluded.raw_object_id,
                ingested_at=excluded.ingested_at
            """,
            (symbol, code, name, SOURCE, classification_date, raw_object_id, now),
        )
        result.inserted += 1
    return result
