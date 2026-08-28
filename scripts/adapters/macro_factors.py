"""macro_factors 采集 CSV → macro_factors 表（消息面 r2 Phase 2）。

采集：scripts/collect/akshare_collect.py --sources macro，因子清单固化在
config/macro_factors.yaml（商品内盘/外盘期货 + 中行外汇牌价，全部 sina 系域名），
落盘 macro/{date}/{run_id}/macro.csv，经 ingest 路由 ("akshare","macro") 进入。

口径（r2 §3.2）：
- close 为来源原始值定点 TEXT，本模块不做任何换算/计算；来源无涨跌幅则
  change_pct 为 NULL（不代算）；
- 主键 (factor_type, code, trade_date)：同日重采 ON CONFLICT DO UPDATE
  （收盘快照随来源修正覆盖，属事实刷新而非新版本）。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from scripts.adapters.common import IngestResult, utc_now

SOURCE = "akshare"


def parse_macro_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """macro.csv（factor_type,code,name,market,trade_date,close,change_pct,unit）
    → macro_factors 主键 upsert。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("macro CSV 无数据行")
        return result

    now = utc_now()
    for rec in rows:
        ftype = (rec.get("factor_type") or "").strip()
        code = (rec.get("code") or "").strip()
        trade_date = (rec.get("trade_date") or "").strip()[:10]
        close = (rec.get("close") or "").strip()
        if not ftype or not code or not trade_date or not close:
            result.skipped += 1
            result.notes.append(f"macro 行缺必填列（{rec}），行级跳过")
            continue
        name = (rec.get("name") or "").strip()
        market = (rec.get("market") or "").strip() or "CN"
        change_pct = (rec.get("change_pct") or "").strip() or None
        unit = (rec.get("unit") or "").strip() or None
        conn.execute(
            """
            INSERT INTO macro_factors (factor_type, code, name, market, trade_date,
                                       close, change_pct, unit, source,
                                       raw_object_id, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(factor_type, code, trade_date) DO UPDATE SET
                name=excluded.name, market=excluded.market,
                close=excluded.close, change_pct=excluded.change_pct,
                unit=excluded.unit, raw_object_id=excluded.raw_object_id,
                ingested_at=excluded.ingested_at
            """,
            (ftype, code, name, market, trade_date, close, change_pct, unit,
             SOURCE, raw_object_id, now),
        )
        result.inserted += 1
    return result
