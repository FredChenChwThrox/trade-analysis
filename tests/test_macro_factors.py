"""macro_factors 测试（消息面 r2 Phase 2）。

锁定：
- migration 0004 建表 + 重跑幂等；
- parse_macro_csv：主键 (factor_type, code, trade_date) upsert——同日重采刷新
  close 而非新增行；缺必填列行级跳过；
- ingest 路由锁定 ("akshare", "macro")。
"""

from __future__ import annotations

import csv

import pytest

from scripts.adapters import macro_factors as mf_adapter
from scripts.adapters.common import ingest_file
from scripts.pipeline import db as pdb


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(tmp_path / "market.db")
    pdb.migrate(c)
    yield c
    c.close()


def _macro_csv(path, close="999.28"):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["factor_type", "code", "name", "market", "trade_date",
                    "close", "change_pct", "unit"])
        w.writerow(["commodity", "AU0", "沪金", "CN", "2026-08-28", close, "", "元/克"])
        w.writerow(["fx", "USDCNY", "美元兑人民币", "CN", "2026-08-28",
                    "678.11", "", "CNY/100USD"])
        w.writerow(["commodity", "BAD0", "坏行", "CN", "", "", "", ""])  # 缺 trade_date/close


def test_migration_0004_and_reapply(tmp_path):
    c = pdb.connect(tmp_path / "m.db")
    applied = pdb.migrate(c)
    assert [n for n in applied if n.startswith("0004")] == ["0004_macro_factors.sql"]
    cols = [r[1] for r in c.execute("PRAGMA table_info(macro_factors)")]
    assert cols == ["factor_type", "code", "name", "market", "trade_date",
                    "close", "change_pct", "unit", "source", "raw_object_id",
                    "ingested_at"]
    assert pdb.migrate(c) == []
    c.close()


def test_parse_macro_csv_upsert_and_skip(conn, tmp_path):
    """upsert：同 (类型,代码,日) 重采刷新 close 不新增行；坏行跳过不冒充。"""
    path = tmp_path / "raw" / "akshare" / "macro" / "2026-08-28" / "run_t" / "macro.csv"
    path.parent.mkdir(parents=True)
    _macro_csv(path)
    r1 = ingest_file(conn, path, source="akshare", data_type="macro",
                     symbol=None, parse=mf_adapter.parse_macro_csv)
    assert r1.status == "ok", r1.summary()
    assert r1.inserted == 2 and r1.skipped == 1  # 2 有效行入库；缺 trade_date/close 的坏行跳过
    rows = conn.execute("SELECT * FROM macro_factors ORDER BY factor_type, code").fetchall()
    assert len(rows) == 2
    au = rows[0]
    assert au["code"] == "AU0" and au["close"] == "999.28"
    assert au["change_pct"] is None and au["source"] == "akshare"
    assert au["unit"] == "元/克"

    # 同日重采（close 变化）→ 刷新原行，行数不变
    _macro_csv(path, close="1001.00")
    r2 = ingest_file(conn, path, source="akshare", data_type="macro",
                     symbol=None, parse=mf_adapter.parse_macro_csv)
    assert r2.status == "ok", r2.summary()
    assert conn.execute("SELECT COUNT(*) FROM macro_factors").fetchone()[0] == 2
    assert conn.execute("SELECT close FROM macro_factors WHERE code='AU0'").fetchone()[0] \
        == "1001.00"


def test_macro_route_registered():
    from scripts.pipeline.ingest import _ROUTES
    assert _ROUTES[("akshare", "macro")] is mf_adapter.parse_macro_csv
