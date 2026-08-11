"""D1.3 adapters 测试（手工 fixture，不依赖网络）。

覆盖：OHLC 校验拒绝坏行、整批回滚、content hash 去重、重复 ingest 幂等、
财报更正产生新 revision、FX 方向统一、stock_actions、预期快照、公告去重、
yahoo 时区换算、指数代码别名、CLI 路由。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.adapters import stock_finance_data as sfd
from scripts.adapters import yahoo_finance as yf
from scripts.adapters.common import ingest_file
from scripts.pipeline import db
from scripts.pipeline.ingest import ingest_paths

# ------------------------------------------------------------- fixture helpers

CN_CAL = {
    # 2026-08-05(三) 08-06(四) 08-07(五) 开市；08-08(六) 08-09(日) 周末
    "2026-08-05": (1, "trading"),
    "2026-08-06": (1, "trading"),
    "2026-08-07": (1, "trading"),
    "2026-08-08": (0, "weekend"),
    "2026-08-09": (0, "weekend"),
}


def add_calendar(conn: sqlite3.Connection, market: str, days: dict) -> None:
    now = db.utc_now()
    for d, (is_open, status) in days.items():
        conn.execute(
            """
            INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                                          session_open, session_close, status,
                                          status_detail, timezone, source, updated_at)
            VALUES (?, ?, ?, 1, NULL, NULL, ?, NULL, 'Asia/Shanghai', 'test', ?)
            """,
            (market, d, is_open, status, now),
        )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "market.db")
    db.migrate(c)
    add_calendar(c, "CN", CN_CAL)
    yield c
    c.close()


def write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


PRICE_GOOD = """open,high,low,close,volume,thscode,time,thsname_cn,thsname_en,currency
100,101,99,100.5,1000,603605.SH,20260806,珀莱雅,NA,CNY
101,102,100,101.5,2000,603605.SH,20260807,珀莱雅,NA,CNY
"""

PRICE_BAD_OHLC = """open,high,low,close,volume,thscode,time,thsname_cn,thsname_en,currency
100,101,99,100.5,1000,603605.SH,20260806,珀莱雅,NA,CNY
101,102,101.5,101.5,2000,603605.SH,20260807,珀莱雅,NA,CNY
"""  # 第二行 low=101.5 > open=101，违反 low<=open

PRICE_ON_WEEKEND = """open,high,low,close,volume,thscode,time,thsname_cn,thsname_en,currency
100,101,99,100.5,1000,603605.SH,20260808,珀莱雅,NA,CNY
"""

IS_V1 = """ths_operating_total_revenue_stock,ths_np_atoopc_stock,ths_basic_eps_stock,ths_dlt_earnings_per_share_stock,thscode,time
10597428522.23,1497751418.11,3.81,3.8,603605.SH,
"""

IS_V1_SAME_FACTS = IS_V1 + "\n"  # 字节不同（hash 不同）但事实一致

IS_V2_CORRECTED = """ths_operating_total_revenue_stock,ths_np_atoopc_stock,ths_basic_eps_stock,ths_dlt_earnings_per_share_stock,thscode,time
10597428522.23,1500000000.00,3.82,3.81,603605.SH,
"""

FORECAST = """ths_fore_np_fy1_stock,ths_fore_mbi_fy1_stock,thscode,time
1626467318.56,11498460937.5,603605.SH,
"""

ANNOUNCEMENT = """time,title,url,seq,thscode
2026-08-06,关于分红的公告,http://example.com/a.pdf,SQ001,603605.SH
2026-08-07,董事会决议公告,http://example.com/b.pdf,SQ002,603605.SH
"""

YF_PRICE = """Date,Open,High,Low,Close,Volume,Dividends,Stock Splits,thscode,thsname_cn,thsname_en,currency
2026-08-05T16:00:00.000Z,479.0,483.2,475.4,478.8,16319939,0.0,0.0,0700.HK,NA,NA,HKD
"""

YF_PRICE_PARTIAL_NAN = """Date,Open,High,Low,Close,Volume,Dividends,Stock Splits,thscode,thsname_cn,thsname_en,currency
2026-08-05T16:00:00.000Z,479.0,,475.4,478.8,16319939,0.0,0.0,0700.HK,NA,NA,HKD
"""

YF_FX = """Date,Open,High,Low,Close,Volume,Dividends,Stock Splits,thscode,thsname_cn,thsname_en,currency
2026-08-05T23:00:00.000Z,1.16,1.162,1.159,1.1614,0,0.0,0.0,CNYHKD=X,NA,NA,HKD
"""

YF_FX_REVERSED = """Date,Open,High,Low,Close,Volume,Dividends,Stock Splits,thscode,thsname_cn,thsname_en,currency
2026-08-05T23:00:00.000Z,0.86,0.87,0.85,0.8609271523,0,0.0,0.0,HKDCNY=X,NA,NA,CNY
"""

YF_ACTIONS = """Date,Dividends,Stock Splits
2014-05-14T16:00:00.000Z,0.0,5.0
2026-05-14T16:00:00.000Z,5.3,0.0
"""

YF_INDEX = """Date,Open,High,Low,Close,Volume,Dividends,Stock Splits,thscode,thsname_cn,thsname_en,currency
2026-08-05T16:00:00.000Z,4792.25,4839.5,4756.7,4792.26,246300,0.0,0.0,000300.SS,NA,NA,CNY
2026-08-06T16:00:00.000Z,,,,,2432365520,0.0,0.0,000300.SS,NA,NA,CNY
"""


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------------- stock_finance_data 行情

def test_price_ingest_ok(conn, tmp_path):
    p = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t/603605.SH.csv", PRICE_GOOD)
    r = ingest_file(conn, p, source="stock_finance_data", data_type="price",
                    symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 2
    rows = conn.execute(
        "SELECT * FROM daily_bars WHERE symbol='603605.SH' ORDER BY trade_date").fetchall()
    assert len(rows) == 2
    assert rows[0]["trade_date"] == "2026-08-06"
    assert rows[0]["amount_raw"] is None  # stock_finance_data 无 amount 列
    assert rows[0]["price_adj_factor"] == 1.0  # 无历史时落 1.0
    assert rows[0]["trading_status"] == "normal"
    assert rows[0]["raw_object_id"] == r.raw_object_id


PRICE_NEXT_DAY = """open,high,low,close,volume,thscode,time,thsname_cn,thsname_en,currency
102,103,101,102.5,3000,603605.SH,20260810,珀莱雅,NA,CNY
"""


def test_price_ingest_new_bar_inherits_factor(conn, tmp_path):
    """新 bar 因子继承上一交易日（2026-08-11 盘后例行 bug：填 1.0 会被
    因子变化检查误判为窗口内除权，触发窗口 CSV 全量重建报 origin 缺失）。"""
    add_calendar(conn, "CN", {"2026-08-10": (1, "trading")})
    p = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t/603605.SH.csv", PRICE_GOOD)
    ingest_file(conn, p, source="stock_finance_data", data_type="price",
                symbol="603605.SH", parse=sfd.parse_price_csv)
    # 模拟 D1.5 重建后的历史因子（分红股最近平台段归一化因子 ≠ 1.0）
    conn.execute("UPDATE daily_bars SET price_adj_factor=1.0584, share_factor=0.5 "
                 "WHERE symbol='603605.SH'")
    conn.commit()
    p2 = write(tmp_path, "raw/stock_finance_data/price/2026-08-10/run_t/603605.SH.csv",
               PRICE_NEXT_DAY)
    r = ingest_file(conn, p2, source="stock_finance_data", data_type="price",
                    symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r.status == "ok", r.summary()
    row = conn.execute(
        "SELECT price_adj_factor, share_factor FROM daily_bars "
        "WHERE symbol='603605.SH' AND trade_date='2026-08-10'").fetchone()
    assert row["price_adj_factor"] == pytest.approx(1.0584)
    assert row["share_factor"] == pytest.approx(0.5)


def test_price_rejects_bad_ohlc_rollback(conn, tmp_path):
    p = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t/bad.csv", PRICE_BAD_OHLC)
    r = ingest_file(conn, p, source="stock_finance_data", data_type="price",
                    symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r.status == "conflict"
    assert r.conflicts == 1
    # 校验失败整批不入库：好行也不进，raw_objects 也不登记
    assert _count(conn, "daily_bars") == 0
    assert _count(conn, "raw_objects") == 0


def test_price_rejects_non_trading_day(conn, tmp_path):
    p = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t/wk.csv", PRICE_ON_WEEKEND)
    r = ingest_file(conn, p, source="stock_finance_data", data_type="price",
                    symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r.status == "conflict"
    assert "非交易日" in r.errors[0]
    assert _count(conn, "daily_bars") == 0


def test_content_hash_dedup(conn, tmp_path):
    p = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t/603605.SH.csv", PRICE_GOOD)
    r1 = ingest_file(conn, p, source="stock_finance_data", data_type="price",
                     symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r1.inserted == 2
    r2 = ingest_file(conn, p, source="stock_finance_data", data_type="price",
                     symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r2.status == "ok"
    assert r2.skipped == 1
    assert "跳过重复解析" in r2.notes[0]
    assert _count(conn, "daily_bars") == 2


def test_reingest_same_facts_idempotent(conn, tmp_path):
    p1 = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t1/603605.SH.csv", PRICE_GOOD)
    p2 = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t2/603605.SH.csv",
               PRICE_GOOD + "\n")  # 新 hash，行内容一致
    ingest_file(conn, p1, source="stock_finance_data", data_type="price",
                symbol="603605.SH", parse=sfd.parse_price_csv)
    r2 = ingest_file(conn, p2, source="stock_finance_data", data_type="price",
                     symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r2.inserted == 0 and r2.skipped == 2 and r2.updated == 0
    assert _count(conn, "data_revisions") == 0


def test_reingest_revision_updates_and_logs(conn, tmp_path):
    p1 = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t1/603605.SH.csv", PRICE_GOOD)
    revised = PRICE_GOOD.replace("101.5,2000", "101.6,2000")  # close 修订
    p2 = write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t2/603605.SH.csv", revised)
    ingest_file(conn, p1, source="stock_finance_data", data_type="price",
                symbol="603605.SH", parse=sfd.parse_price_csv)
    r2 = ingest_file(conn, p2, source="stock_finance_data", data_type="price",
                     symbol="603605.SH", parse=sfd.parse_price_csv)
    assert r2.updated == 1 and r2.skipped == 1
    close = conn.execute(
        "SELECT close_raw FROM daily_bars WHERE symbol='603605.SH' AND trade_date='2026-08-07'"
    ).fetchone()[0]
    assert close == pytest.approx(101.6)
    rev = conn.execute("SELECT * FROM data_revisions WHERE table_name='daily_bars'").fetchall()
    assert len(rev) == 1
    assert "101.5" in rev[0]["old_value"] and "101.6" in rev[0]["new_value"]


# ------------------------------------------------------------- 财报

def test_financials_ingest_and_revision(conn, tmp_path):
    p1 = write(tmp_path, "raw/stock_finance_data/financials/2026-08-09/run_t/603605.SH_is_20251231.csv", IS_V1)
    r1 = ingest_file(conn, p1, source="stock_finance_data", data_type="financials",
                     symbol="603605.SH", parse=sfd.parse_financials_csv)
    assert r1.inserted == 1
    assert r1.status == "incomplete"  # 无披露时间 → 降级
    row = conn.execute("SELECT * FROM financial_reports").fetchone()
    assert row["period_end"] == "2025-12-31"
    assert row["period_type"] == "annual"
    assert row["revision"] == 1
    fact = conn.execute("SELECT * FROM financial_facts WHERE report_id=?",
                        (row["report_id"],)).fetchone()
    assert fact["net_profit_attr"] == "1497751418.11"
    assert fact["shares_issued_end"] is None  # 利润表无股本列

    # 同内容新文件（新 hash，事实一致）→ 跳过，不产生新 revision
    p_same = write(tmp_path, "raw/stock_finance_data/financials/2026-08-09/run_t2/603605.SH_is_20251231.csv", IS_V1_SAME_FACTS)
    r_same = ingest_file(conn, p_same, source="stock_finance_data", data_type="financials",
                         symbol="603605.SH", parse=sfd.parse_financials_csv)
    assert r_same.skipped == 1 and r_same.inserted == 0
    assert _count(conn, "financial_reports") == 1

    # 更正 → 新增 revision=2，旧版本保留
    p2 = write(tmp_path, "raw/stock_finance_data/financials/2026-08-09/run_t3/603605.SH_is_20251231.csv", IS_V2_CORRECTED)
    r2 = ingest_file(conn, p2, source="stock_finance_data", data_type="financials",
                     symbol="603605.SH", parse=sfd.parse_financials_csv)
    assert r2.inserted == 1
    rows = conn.execute(
        "SELECT revision FROM financial_reports WHERE symbol='603605.SH' ORDER BY revision"
    ).fetchall()
    assert [r["revision"] for r in rows] == [1, 2]


def test_financials_quarterly_type(conn, tmp_path):
    p = write(tmp_path, "raw/stock_finance_data/financials/2026-08-09/run_t/603605.SH_is_20260331.csv", IS_V1)
    ingest_file(conn, p, source="stock_finance_data", data_type="financials",
                symbol="603605.SH", parse=sfd.parse_financials_csv)
    row = conn.execute("SELECT * FROM financial_reports").fetchone()
    assert row["period_type"] == "quarterly"
    assert row["fiscal_year"] == 2026
    assert row["is_cumulative"] == 1


# ------------------------------------------------------------- 公告

def test_announcement_ingest_and_dedup(conn, tmp_path):
    p = write(tmp_path, "raw/stock_finance_data/announcement/2026-08-09/run_t/603605.SH.csv", ANNOUNCEMENT)
    r = ingest_file(conn, p, source="stock_finance_data", data_type="announcement",
                    symbol="603605.SH", parse=sfd.parse_announcement_csv)
    assert r.inserted == 2
    ev = conn.execute("SELECT * FROM events ORDER BY published_at").fetchall()
    assert len(ev) == 2
    assert ev[0]["event_type"] == "announcement"
    assert ev[0]["source_external_id"] == "SQ001"
    assert _count(conn, "event_symbols") == 2
    # 新文件（新 hash）同公告 → 按 source_external_id 去重跳过
    p2 = write(tmp_path, "raw/stock_finance_data/announcement/2026-08-09/run_t2/603605.SH.csv",
               ANNOUNCEMENT + "\n")
    r2 = ingest_file(conn, p2, source="stock_finance_data", data_type="announcement",
                     symbol="603605.SH", parse=sfd.parse_announcement_csv)
    assert r2.inserted == 0 and r2.skipped == 2
    assert _count(conn, "events") == 2


# ------------------------------------------------------------- 预期

def test_forecast_snapshot(conn, tmp_path):
    p = write(tmp_path, "raw/stock_finance_data/forecast/2026-08-09/run_t/603605.SH.csv", FORECAST)
    r = ingest_file(conn, p, source="stock_finance_data", data_type="forecast",
                    symbol="603605.SH", parse=sfd.parse_forecast_csv)
    assert r.inserted == 1
    rows = conn.execute("SELECT * FROM forecasts").fetchall()
    assert len(rows) == 1
    assert "1626467318" in rows[0]["payload_json"]
    # 第二次抓取（新内容）→ 新快照行，历史快照保留
    p2 = write(tmp_path, "raw/stock_finance_data/forecast/2026-08-09/run_t2/603605.SH.csv",
               FORECAST.replace("1626467318.56", "1626467318.57"))
    ingest_file(conn, p2, source="stock_finance_data", data_type="forecast",
                symbol="603605.SH", parse=sfd.parse_forecast_csv)
    assert _count(conn, "forecasts") == 2


# ------------------------------------------------------------- yahoo 行情 / FX / stock_actions / 指数

def test_yahoo_price_tz_conversion_and_missing_hk_calendar(conn, tmp_path):
    p = write(tmp_path, "raw/yahoo_finance/price/2026-08-09/run_t/0700.HK.csv", YF_PRICE)
    r = ingest_file(conn, p, source="yahoo_finance", data_type="price",
                    symbol="0700.HK", parse=yf.parse_price_csv)
    assert r.inserted == 1
    # HK 日历缺失 → incomplete 原因（§2.5），但仍入库
    assert r.status == "incomplete"
    assert any("trading_calendar 缺失" in x for x in r.incomplete_reasons)
    row = conn.execute("SELECT * FROM daily_bars WHERE symbol='0700.HK'").fetchone()
    # 2026-08-05T16:00Z = 2026-08-06 00:00 HKT → 本地交易日 2026-08-06
    assert row["trade_date"] == "2026-08-06"
    assert row["market"] == "HK"
    assert row["currency"] == "HKD"


def test_yahoo_price_rejects_partial_nan(conn, tmp_path):
    p = write(tmp_path, "raw/yahoo_finance/price/2026-08-09/run_t/0700.HK.csv", YF_PRICE_PARTIAL_NAN)
    r = ingest_file(conn, p, source="yahoo_finance", data_type="price",
                    symbol="0700.HK", parse=yf.parse_price_csv)
    assert r.status == "conflict"
    assert _count(conn, "daily_bars") == 0


def test_fx_ingest(conn, tmp_path):
    p = write(tmp_path, "raw/yahoo_finance/fx/2026-08-09/run_t/CNYHKD=X.csv", YF_FX)
    r = ingest_file(conn, p, source="yahoo_finance", data_type="fx",
                    symbol="CNYHKD=X", parse=yf.parse_fx_csv)
    assert r.inserted == 1
    row = conn.execute("SELECT * FROM fx_rates").fetchone()
    assert row["from_currency"] == "CNY" and row["to_currency"] == "HKD"
    # 2026-08-05T23:00Z → HKT 次日 07:00 → rate_date 2026-08-06
    assert row["rate_date"] == "2026-08-06"
    assert row["rate"].startswith("1.1614")


def test_fx_direction_inversion(conn, tmp_path):
    """来源只有反向对时取倒数入库，方向统一为财务币种→交易币种（§3.7）。"""
    p = write(tmp_path, "raw/yahoo_finance/fx/2026-08-09/run_t/HKDCNY=X.csv", YF_FX_REVERSED)
    r = ingest_file(
        conn, p, source="yahoo_finance", data_type="fx", symbol="HKDCNY=X",
        parse=lambda c, path, roid, res: yf.parse_fx_csv(
            c, path, roid, res, direction=("CNY", "HKD")))
    assert r.inserted == 1
    row = conn.execute("SELECT * FROM fx_rates").fetchone()
    assert row["from_currency"] == "CNY" and row["to_currency"] == "HKD"
    assert float(row["rate"]) == pytest.approx(1 / 0.8609271523, rel=1e-9)


def test_stock_actions(conn, tmp_path):
    p = write(tmp_path, "raw/yahoo_finance/stock_actions/2026-08-09/run_t/0700.HK.csv", YF_ACTIONS)
    r = ingest_file(conn, p, source="yahoo_finance", data_type="stock_actions",
                    symbol="0700.HK", parse=yf.parse_stock_actions_csv)
    assert r.inserted == 2
    rows = {row["action_type"]: row for row in conn.execute("SELECT * FROM corporate_actions")}
    assert rows["split"]["split_ratio"] == "5.0"
    assert rows["split"]["ex_date"] == "2014-05-15"  # 16:00Z → HKT 次日
    assert rows["cash_dividend"]["cash_per_share"] == "5.3"
    assert rows["cash_dividend"]["ex_date"] == "2026-05-15"
    # 重复抓取（新 hash 同内容）→ 幂等跳过
    p2 = write(tmp_path, "raw/yahoo_finance/stock_actions/2026-08-09/run_t2/0700.HK.csv",
               YF_ACTIONS + "\n")
    r2 = ingest_file(conn, p2, source="yahoo_finance", data_type="stock_actions",
                     symbol="0700.HK", parse=yf.parse_stock_actions_csv)
    assert r2.inserted == 0
    assert _count(conn, "corporate_actions") == 2


def test_yahoo_index_alias_and_empty_bar_skip(conn, tmp_path):
    p = write(tmp_path, "raw/yahoo_finance/index/2026-08-09/run_t/000300.SS.csv", YF_INDEX)
    r = ingest_file(conn, p, source="yahoo_finance", data_type="index",
                    symbol="000300.SS", parse=yf.parse_index_csv)
    assert r.conflicts == 0
    assert r.inserted == 1 and r.skipped == 1  # 残缺 bar 行级跳过
    row = conn.execute("SELECT * FROM index_bars").fetchone()
    assert row["index_code"] == "000300.SH"  # yahoo 代码别名归一
    assert row["trade_date"] == "2026-08-06"  # 16:00Z → 次日（上海时区）


# ------------------------------------------------------------- CLI 路由

def test_cli_routing_and_forward_skip(conn, tmp_path):
    write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t/603605.SH.csv", PRICE_GOOD)
    write(tmp_path, "raw/stock_finance_data/price/2026-08-09/run_t/603605.SH_forward.csv",
          PRICE_GOOD.replace("100.5", "95.5"))  # 前复权文件不入 daily_bars
    write(tmp_path, "raw/yahoo_finance/fx/2026-08-09/run_t/CNYHKD=X.csv", YF_FX)
    results, total = ingest_paths(conn, [str(tmp_path / "raw")])
    assert total.errors == [] and total.conflicts == 0
    assert _count(conn, "daily_bars") == 2
    assert _count(conn, "fx_rates") == 1
    forward = next(r for r in results if r.file_path.endswith("603605.SH_forward.csv"))
    assert forward.skipped == 1
    assert any("D1.5" in n for n in forward.notes)
