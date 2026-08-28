"""flow 层事件测试（消息面 r2 Phase 2）。

锁定：
- lhb/dzjy CSV → events(event_type='flow', scope='flow', source_tier=3) +
  event_symbols 关联；event_id 确定性 → 重跑幂等跳过；
- available_at = published_at（盘后数据当日可得）；
- 未知 stem 跳过；ingest 路由锁定 ("akshare", "flow")。
"""

from __future__ import annotations

import csv

import pytest

from scripts.adapters import flow_events as flow_adapter
from scripts.adapters.common import ingest_file
from scripts.pipeline import db as pdb


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(tmp_path / "market.db")
    pdb.migrate(c)
    yield c
    c.close()


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_parse_lhb_csv_flow_events(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "flow" / "2026-08-28" / "run_t" / "lhb.csv"
    path.parent.mkdir(parents=True)
    _write_csv(path,
               ["symbol", "trade_date", "reasons", "close", "pct_chg",
                "net_buy", "net_buy_ratio"],
               [["601899.SH", "2026-08-27", "日涨幅偏离值达7%的证券", "34.57",
                 "0.2901", "-123456.78", "-0.5"],
                ["601899.SH", "2026-08-28", "日振幅值达15%的证券", "35.00",
                 "1.24", "8000000.00", "2.1"]])
    r = ingest_file(conn, path, source="akshare", data_type="flow",
                    symbol=None, parse=flow_adapter.parse_flow_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 2
    ev = conn.execute(
        "SELECT * FROM events WHERE event_type='flow' ORDER BY published_at").fetchall()
    assert len(ev) == 2
    e = ev[0]
    assert e["scope"] == "flow" and e["source_tier"] == 3 and e["source"] == "akshare"
    assert "龙虎榜上榜（日涨幅偏离值达7%的证券）" in e["title"]
    assert "净卖出" in e["summary"] and "占成交比 -0.5%" in e["summary"]
    assert e["available_at"] == e["published_at"]
    assert e["event_id"].startswith("evt_")
    sym = conn.execute("SELECT symbol FROM event_symbols WHERE event_id=?",
                       (e["event_id"],)).fetchone()
    assert sym["symbol"] == "601899.SH"

    # 幂等：同内容重跑零新增
    r2 = ingest_file(conn, path, source="akshare", data_type="flow",
                     symbol=None, parse=flow_adapter.parse_flow_csv)
    assert r2.inserted == 0 and r2.skipped >= 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='flow'").fetchone()[0] == 2


def test_parse_dzjy_csv_flow_events(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "flow" / "2026-08-28" / "run_t" / "dzjy.csv"
    path.parent.mkdir(parents=True)
    _write_csv(path,
               ["symbol", "trade_date", "close", "pct_chg", "price",
                "premium_rate", "volume", "amount", "buy_branch", "sell_branch"],
               [["601899.SH", "2026-08-27", "34.57", "0.2901", "34.57", "0.0",
                 "775700", "26946100", "摩根大通证券(中国)有限公司上海银城中路证券营业部",
                 "机构专用"]])
    r = ingest_file(conn, path, source="akshare", data_type="flow",
                    symbol=None, parse=flow_adapter.parse_flow_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 1
    e = conn.execute("SELECT * FROM events WHERE event_type='flow'").fetchone()
    assert "大宗交易：成交价 34.57（折溢价 0.0%）" in e["title"]
    assert "成交额 0.27 亿元" in e["summary"] and "机构专用" in e["summary"]
    assert e["scope"] == "flow" and e["source_tier"] == 3

    # 第二笔（不同成交量）→ 不同 event_id，独立成行
    _write_csv(path,
               ["symbol", "trade_date", "close", "pct_chg", "price",
                "premium_rate", "volume", "amount", "buy_branch", "sell_branch"],
               [["601899.SH", "2026-08-27", "34.57", "0.2901", "34.57", "0.0",
                 "500000", "17300000", "买方B", "卖方B"]])
    r2 = ingest_file(conn, path, source="akshare", data_type="flow",
                     symbol=None, parse=flow_adapter.parse_flow_csv)
    assert r2.inserted == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='flow'").fetchone()[0] == 2


def test_parse_flow_unknown_stem_skipped(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "flow" / "2026-08-28" / "run_t" / "other.csv"
    path.parent.mkdir(parents=True)
    path.write_text("x\n", encoding="utf-8")
    r = ingest_file(conn, path, source="akshare", data_type="flow",
                    symbol=None, parse=flow_adapter.parse_flow_csv)
    assert r.status == "ok"
    assert any("未知文件 stem" in n for n in r.notes)


def test_flow_route_registered():
    from scripts.pipeline.ingest import _ROUTES
    assert _ROUTES[("akshare", "flow")] is flow_adapter.parse_flow_csv
