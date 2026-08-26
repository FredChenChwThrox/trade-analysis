"""akshare adapter 测试：采集落盘 CSV → 入库字段与库 schema 对齐（复用 upsert_*/tdx financials）。"""

import csv
import hashlib

import pytest

from scripts.adapters import akshare as ak_adapter
from scripts.adapters.common import ingest_file
from scripts.pipeline import db as pdb


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(tmp_path / "market.db")
    pdb.migrate(c)
    pdb.seed(c)  # watchlist 6 只 + CN 2023-2026 日历
    yield c
    c.close()


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)


# ---------------------------------------------------------------- price → daily_bars

def test_parse_price_csv_aligns_daily_bars(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "price" / "2026-08-25" / "run_ak" / "603605.SH.csv"
    path.parent.mkdir(parents=True)
    _write_csv(path, ["thscode", "time", "open", "high", "low", "close", "volume", "amount", "currency"], [
        ["603605.SH", "20260803", "10.0", "10.5", "9.9", "10.2", "1000000", "10000000", "CNY"],
        ["603605.SH", "20260804", "10.5", "11.0", "10.4", "10.8", "1200000", "13000000", "CNY"],
    ])
    r = ingest_file(conn, path, source="akshare", data_type="price",
                    symbol="603605.SH", parse=ak_adapter.parse_price_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 2
    bar = conn.execute(
        "SELECT * FROM daily_bars WHERE symbol='603605.SH' AND trade_date='2026-08-04'").fetchone()
    assert bar["open_raw"] == 10.5
    assert bar["close_raw"] == 10.8
    assert bar["volume_raw"] == 1200000          # 已是"股"（采集器换算后落盘）
    assert bar["amount_raw"] == 13000000
    assert bar["source"] == "akshare"
    assert bar["trading_status"] == "normal"


def test_parse_price_csv_bad_row_conflict(conn, tmp_path):
    path = tmp_path / "bad.csv"
    _write_csv(path, ["thscode", "time", "open", "high", "low", "close", "volume", "amount", "currency"], [
        ["603605.SH", "20260803", "12.0", "10.0", "9.9", "11.0", "100", "1", "CNY"],  # low<=open 违反
    ])
    r = ingest_file(conn, path, source="akshare", data_type="price",
                    symbol="603605.SH", parse=ak_adapter.parse_price_csv)
    assert r.status == "conflict"
    assert conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == 0


# ---------------------------------------------------------------- financials → financial_reports/facts

def test_parse_financials_csv_published_at_aligned(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "financials" / "2026-08-25" / "run_ak" / "603605.SH_is_20251231.csv"
    path.parent.mkdir(parents=True)
    _write_csv(path, ["code", "setcode", "period_end", "fiscal_year", "revenue",
                      "net_profit_attr", "eps_basic", "eps_diluted", "currency",
                      "unit", "is_cumulative", "published_at"], [
        ["603605", "1", "2025-12-31", "2025", "10700000000",
         "1500000000", "3.80", "3.78", "CNY", "yuan", "1", "2026-04-15"],
    ])
    r = ingest_file(conn, path, source="akshare", data_type="financials",
                    symbol="603605.SH", parse=ak_adapter.parse_financials_csv)
    assert r.status == "ok", r.summary()
    rep = conn.execute(
        "SELECT * FROM financial_reports WHERE symbol='603605.SH' AND period_end='2025-12-31'").fetchone()
    assert rep is not None
    assert rep["published_at"] is not None        # akshare NOTICE_DATE 补上披露日
    assert rep["published_at"].startswith("2026-04-14T") or rep["published_at"].startswith("2026-04-15T")
    assert rep["published_tz"] == "Asia/Shanghai"
    assert rep["available_at"] > rep["published_at"]   # 下一开市交易日生效（§2.1）
    assert rep["unit"] == "yuan"
    assert rep["is_cumulative"] == 1
    fact = conn.execute(
        "SELECT * FROM financial_facts WHERE report_id=?", (rep["report_id"],)).fetchone()
    assert fact["revenue"] == "10700000000"
    assert fact["net_profit_attr"] == "1500000000"
    assert fact["eps_basic"] == "3.80"

    # 幂等：同内容重跑 → skipped，不新增 revision
    r2 = ingest_file(conn, path, source="akshare", data_type="financials",
                     symbol="603605.SH", parse=ak_adapter.parse_financials_csv)
    assert r2.status == "ok"
    assert r2.inserted == 0
    assert r2.skipped == 1
    assert conn.execute(
        "SELECT MAX(revision) FROM financial_reports WHERE symbol='603605.SH' "
        "AND period_end='2025-12-31'").fetchone()[0] == 1

    # 内容变化 → 新增 revision（不覆盖）
    path2 = tmp_path / "rev2.csv"
    _write_csv(path2, ["code", "setcode", "period_end", "fiscal_year", "revenue",
                       "net_profit_attr", "eps_basic", "eps_diluted", "currency",
                       "unit", "is_cumulative", "published_at"], [
        ["603605", "1", "2025-12-31", "2025", "10700000001",
         "1500000000", "3.80", "3.78", "CNY", "yuan", "1", "2026-04-15"],
    ])
    r3 = ingest_file(conn, path2, source="akshare", data_type="financials",
                     symbol="603605.SH", parse=ak_adapter.parse_financials_csv)
    assert r3.status == "ok"
    assert r3.inserted == 1
    assert conn.execute(
        "SELECT MAX(revision) FROM financial_reports WHERE symbol='603605.SH' "
        "AND period_end='2025-12-31'").fetchone()[0] == 2


def test_parse_financials_csv_missing_published_at_degraded(conn, tmp_path):
    path = tmp_path / "nopub.csv"
    _write_csv(path, ["code", "setcode", "period_end", "fiscal_year", "revenue",
                      "net_profit_attr", "eps_basic", "eps_diluted", "currency",
                      "unit", "is_cumulative", "published_at"], [
        ["603605", "1", "2025-12-31", "2025", "1", "1", "1", "1", "CNY", "yuan", "1", ""],
    ])
    r = ingest_file(conn, path, source="akshare", data_type="financials",
                    symbol="603605.SH", parse=ak_adapter.parse_financials_csv)
    assert r.status == "incomplete"  # 无披露日 → 降级（§2.1），不冲突
    rep = conn.execute(
        "SELECT * FROM financial_reports WHERE symbol='603605.SH' AND period_end='2025-12-31'").fetchone()
    assert rep["published_at"] is None


# ---------------------------------------------------------------- index → index_bars

def test_parse_index_csv_aligned(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "index" / "2026-08-25" / "run_ak" / "000300.SH.csv"
    path.parent.mkdir(parents=True)
    _write_csv(path, ["thscode", "time", "open", "high", "low", "close", "volume", "amount", "currency"], [
        ["000300.SH", "20260803", "4000.0", "4020.0", "3990.0", "4015.0", "300000000", "", "CNY"],
    ])
    r = ingest_file(conn, path, source="akshare", data_type="index",
                    symbol="000300.SH", parse=ak_adapter.parse_index_csv)
    assert r.status == "ok", r.summary()
    bar = conn.execute("SELECT * FROM index_bars WHERE index_code='000300.SH'").fetchone()
    assert bar["close"] == 4015.0
    assert bar["source"] == "akshare"


def test_parse_index_csv_bad_row_skipped(conn, tmp_path):
    """新浪恒生 open=0 之类的源缺陷行：行级跳过，不整批回滚（§2.5）。"""
    path = tmp_path / "badindex.csv"
    _write_csv(path, ["thscode", "time", "open", "high", "low", "close", "volume", "amount", "currency"], [
        ["000300.SH", "20260803", "4000.0", "4020.0", "3990.0", "4015.0", "300000000", "", "CNY"],
        ["000300.SH", "20260804", "0.0", "4030.0", "4005.0", "4025.0", "310000000", "", "CNY"],  # open=0 < low
    ])
    r = ingest_file(conn, path, source="akshare", data_type="index",
                    symbol="000300.SH", parse=ak_adapter.parse_index_csv)
    assert r.status == "ok"
    assert r.inserted == 1
    assert r.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM index_bars").fetchone()[0] == 1


# ---------------------------------------------------------------- telegraph → events/event_symbols

def _telegraph_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event_type", "published_at", "published_tz", "title",
                    "summary", "content", "source_external_id", "content_hash"])
        for r in rows:
            w.writerow(r)


def test_parse_telegraph_csv_events_and_symbols(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "telegraph" / "2026-08-25" / "run_ak" / "telegraph_2026-08-25.csv"
    path.parent.mkdir(parents=True)
    ch = hashlib.sha256("珀莱雅：上半年净利同比增长".encode()).hexdigest()
    _telegraph_csv(path, [
        ["news", "2026-08-25T02:30:00+00:00", "Asia/Shanghai", "珀莱雅：上半年净利同比增长",
         "珀莱雅（603605.SH）发布中报。", "财联社8月25日电，珀莱雅（603605.SH）发布中报。",
         "cls_1787730000", ch],
    ])
    r = ingest_file(conn, path, source="akshare", data_type="telegraph",
                    symbol=None, parse=ak_adapter.parse_telegraph_csv)
    assert r.status == "ok", r.summary()
    ev = conn.execute("SELECT * FROM events WHERE source='akshare'").fetchall()
    assert len(ev) == 1
    assert ev[0]["event_type"] == "news"
    assert ev[0]["published_at"] == "2026-08-25T02:30:00+00:00"
    assert ev[0]["published_tz"] == "Asia/Shanghai"
    assert ev[0]["title"] == "珀莱雅：上半年净利同比增长"
    assert ev[0]["available_at"] is not None
    # event_symbols 按 watchlist 名称/别名/symbol 匹配
    links = conn.execute(
        "SELECT symbol FROM event_symbols WHERE event_id=?", (ev[0]["event_id"],)).fetchall()
    assert ("603605.SH",) in {tuple(x) for x in links}

    # 幂等：同 source_external_id 重跑 → skipped
    r2 = ingest_file(conn, path, source="akshare", data_type="telegraph",
                     symbol=None, parse=ak_adapter.parse_telegraph_csv)
    assert r2.status == "ok"
    assert r2.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_parse_telegraph_csv_empty_title_row_skipped(conn, tmp_path):
    """无标题（如图片快讯）行级跳过，其余行正常入库（§2.5）。"""
    path = tmp_path / "telegraph_mixed.csv"
    _telegraph_csv(path, [
        ["news", "2026-08-25T02:30:00+00:00", "Asia/Shanghai", "",
         "", "财联社8月25日电，图片快讯。", "cls_1", "h1"],
        ["news", "2026-08-25T02:31:00+00:00", "Asia/Shanghai", "海天味业：中报披露",
         "海天味业发布中报。", "财联社8月25日电，海天味业（603288.SH）发布中报。",
         "cls_2", "h2"],
    ])
    r = ingest_file(conn, path, source="akshare", data_type="telegraph",
                    symbol=None, parse=ak_adapter.parse_telegraph_csv)
    assert r.status == "ok"
    assert r.inserted == 1
    assert r.skipped == 1
    ev = conn.execute("SELECT * FROM events WHERE source='akshare'").fetchall()
    assert len(ev) == 1
    assert ev[0]["title"] == "海天味业：中报披露"
    links = conn.execute(
        "SELECT symbol FROM event_symbols WHERE event_id=?", (ev[0]["event_id"],)).fetchall()
    assert ("603288.SH",) in {tuple(x) for x in links}  # 名称 + 代码 双匹配


# ---------------------------------------------------------------- forecast → forecasts

_FORECAST_COLS = [
    "ths_fore_np_fy1_stock", "ths_fore_np_fy2_stock", "ths_fore_np_fy3_stock",
    "ths_fore_np_in12m_stock", "ths_fore_np_yoy_stock",
    "ths_fore_mbi_fy1_stock", "ths_fore_mbi_fy2_stock", "ths_fore_mbi_fy3_stock",
    "ths_fore_mbi_yoy_stock", "thscode", "time",
    "ak_np_orgs_fy1", "ak_np_orgs_fy2", "ak_np_orgs_fy3",
    "ak_np_min_fy1", "ak_np_max_fy1", "ak_np_min_fy2", "ak_np_max_fy2",
    "ak_np_min_fy3", "ak_np_max_fy3",
    "ak_eps_fy1", "ak_eps_fy2", "ak_eps_fy3",
]


def test_parse_forecast_csv_aligned(conn, tmp_path):
    path = tmp_path / "raw" / "akshare" / "forecast" / "2026-08-26" / "run_ak" / "603605.SH.csv"
    path.parent.mkdir(parents=True)
    row = [""] * len(_FORECAST_COLS)
    row[0], row[1], row[2] = "32917000000", "36837000000", "41941000000"
    row[9] = "603605.SH"
    row[11] = "23"  # ak_np_orgs_fy1
    _write_csv(path, _FORECAST_COLS, [row])
    r = ingest_file(conn, path, source="akshare", data_type="forecast",
                    symbol="603605.SH", parse=ak_adapter.parse_forecast_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 1
    fc = conn.execute("SELECT * FROM forecasts WHERE symbol='603605.SH'").fetchone()
    assert fc["source"] == "akshare"  # 来源标注为 akshare，非 stock_finance_data
    import json
    payload = json.loads(fc["payload_json"])
    rec = payload["rows"][0]
    assert rec["ths_fore_np_fy1_stock"] == "32917000000"
    assert rec["ak_np_orgs_fy1"] == "23"  # 附加列全量保留


# ---------------------------------------------------------------- stock_info → share_capital_events

_STOCK_INFO_COLS = ["thscode", "ths_total_shares_stock", "ak_change_date",
                    "ak_change_reason", "ak_float_a_shares"]


def _seed_daily_bar(conn, symbol, trade_date):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, amount_raw, currency, trading_status,
            source, updated_at)
        VALUES (?, ?, 'CN', 10, 10.5, 9.9, 10.2, 1000000, 10000000, 'CNY',
                'normal', 'test', '2026-08-26T00:00:00+00:00')
        """,
        (symbol, trade_date),
    )


def _stock_info_csv(path, shares, symbol="603605.SH"):
    _write_csv(path, _STOCK_INFO_COLS,
               [[symbol, shares, "20250716", "回购", "17460840000.0"]])


def test_parse_stock_info_csv_snapshot(conn, tmp_path):
    _seed_daily_bar(conn, "603605.SH", "2023-08-10")
    path = tmp_path / "raw" / "akshare" / "stock_info" / "2026-08-26" / "run_ak" / "603605.SH.csv"
    path.parent.mkdir(parents=True)
    _stock_info_csv(path, "21394310176")
    r = ingest_file(conn, path, source="akshare", data_type="stock_info",
                    symbol="603605.SH", parse=ak_adapter.parse_stock_info_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 1
    ev = conn.execute(
        "SELECT * FROM share_capital_events WHERE symbol='603605.SH'").fetchone()
    assert ev["event_type"] == "snapshot_group_total"
    assert ev["share_count_type"] == "group_total"
    assert ev["shares_issued_after"] == "21394310176"
    assert ev["effective_at"] == "2023-08-10"  # 推导自 daily_bars 最早交易日
    assert ev["source"] == "akshare stock_zh_a_gbjg_em"


def test_parse_stock_info_csv_no_daily_bars_conflict(conn, tmp_path):
    path = tmp_path / "stock_info.csv"
    _stock_info_csv(path, "21394310176")
    r = ingest_file(conn, path, source="akshare", data_type="stock_info",
                    symbol="603605.SH", parse=ak_adapter.parse_stock_info_csv)
    assert r.status == "conflict"
    assert "daily_bars 为空" in r.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM share_capital_events").fetchone()[0] == 0


def test_parse_stock_info_csv_cross_source_same_shares_skipped(conn, tmp_path):
    """kimi 源已有同 effective_at 同股本 group_total 快照 → 幂等跳过（源可切换不重复写）。"""
    _seed_daily_bar(conn, "603605.SH", "2023-08-10")
    conn.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at, event_type,
            share_change, shares_issued_after, share_count_type, details_json, source,
            raw_object_id, created_at)
        VALUES ('603605.SH', '2023-08-10', '2026-08-17T00:00:00+00:00',
                'snapshot_group_total', NULL, '21394310176', 'group_total', '{}',
                'stock_finance_data get_stock_info', NULL, '2026-08-17T00:00:00+00:00')
        """)
    conn.commit()  # 预置行先落盘，避免被 ingest_file 事务回滚连带撤销
    path = tmp_path / "stock_info.csv"
    _stock_info_csv(path, "21394310176")
    r = ingest_file(conn, path, source="akshare", data_type="stock_info",
                    symbol="603605.SH", parse=ak_adapter.parse_stock_info_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 0 and r.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM share_capital_events").fetchone()[0] == 1


def test_parse_stock_info_csv_cross_source_conflict(conn, tmp_path):
    """kimi 源已有同 effective_at 但股本不同 → conflict 交人工核对（§3.2）。"""
    _seed_daily_bar(conn, "603605.SH", "2023-08-10")
    conn.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at, event_type,
            share_change, shares_issued_after, share_count_type, details_json, source,
            raw_object_id, created_at)
        VALUES ('603605.SH', '2023-08-10', '2026-08-17T00:00:00+00:00',
                'snapshot_group_total', NULL, '21000000000', 'group_total', '{}',
                'stock_finance_data get_stock_info', NULL, '2026-08-17T00:00:00+00:00')
        """)
    conn.commit()  # 预置行先落盘，避免被 ingest_file 事务回滚连带撤销
    path = tmp_path / "stock_info.csv"
    _stock_info_csv(path, "21394310176")
    r = ingest_file(conn, path, source="akshare", data_type="stock_info",
                    symbol="603605.SH", parse=ak_adapter.parse_stock_info_csv)
    assert r.status == "conflict"
    assert "股本冲突" in r.errors[0]
    # 整批回滚：akshare 行未写入
    rows = conn.execute("SELECT source FROM share_capital_events").fetchall()
    assert [x["source"] for x in rows] == ["stock_finance_data get_stock_info"]
