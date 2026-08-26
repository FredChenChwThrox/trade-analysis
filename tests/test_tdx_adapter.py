"""tdx adapter 测试（手工 fixture，不依赖网络）。

覆盖：
- K 线 OHLC 校验、amount 非空、复权因子继承
- 港股 setcode=31 → 00700.HK 入库（HK 日历缺失走 incomplete 降级）
- tqflag=1/2 前/后复权文件不入 daily_bars
- 指数 setcode=62 → 000300.SH 入 index_bars
- 公告 title|date 哈希去重、symbol 关联
- 估值快照入 share_capital_events（group_total_tdx，不参与 PE 取数）
- ingest CLI 路由按路径推断 source/data_type
- 坏 OHLC 整批回滚、非交易日拒绝、content hash 幂等
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.adapters import tdx
from scripts.adapters.common import ingest_file
from scripts.pipeline import db
from scripts.pipeline.ingest import ingest_paths

# ------------------------------------------------------------- fixture helpers

CN_CAL = {
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


def _count(conn, table, where: str = "") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


# ------------------------------------------------------------- fixture CSV

# 通达信 K 线 CSV（A 股，tqflag=0 不复权，含 amount）
KLINE_A = """code,setcode,data,open,high,low,close,volume,amount,name,period,tqflag
603605,1,20260805,100.0,101.0,99.0,100.5,1000,100500.0,珀莱雅,4,0
603605,1,20260806,101.0,102.0,100.0,101.5,2000,203000.0,珀莱雅,4,0
603605,1,20260807,101.5,103.0,101.0,102.8,1500,154200.0,珀莱雅,4,0
"""

# 通达信 K 线 CSV（港股 00700，tqflag=0）
KLINE_HK = """code,setcode,data,open,high,low,close,volume,amount,name,period,tqflag
00700,31,20260805,479.0,483.2,475.4,478.8,16319939,7819301200.0,腾讯,4,0
"""

# 通达信前复权文件（tqflag=1，应被跳过不入 daily_bars）
KLINE_FWD = """code,setcode,data,open,high,low,close,volume,amount,name,period,tqflag
603605,1,20260805,105.83,106.06,104.45,105.03,1000,100500.0,珀莱雅,4,1
"""

# 坏 OHLC（low > open）
KLINE_BAD_OHLC = """code,setcode,data,open,high,low,close,volume,amount,name,period,tqflag
603605,1,20260805,100.0,101.0,99.0,100.5,1000,100500.0,珀莱雅,4,0
603605,1,20260806,101.0,102.0,101.5,101.5,2000,203000.0,珀莱雅,4,0
"""

# 周末 bar
KLINE_WEEKEND = """code,setcode,data,open,high,low,close,volume,amount,name,period,tqflag
603605,1,20260808,100.0,101.0,99.0,100.5,1000,100500.0,珀莱雅,4,0
"""

# 指数 CSV（沪深300，setcode=62，unit=1 指数 volume 不换算）
INDEX_300 = """code,setcode,data,open,high,low,close,volume,amount,name,unit
000300,62,20260805,4660.0,4675.0,4657.0,4663.8,200000000,932760000000.0,沪深300,1
000300,62,20260806,4663.8,4690.0,4650.0,4680.5,180000000,842400000000.0,沪深300,1
"""

# 公告 CSV（无 uuid，按 title|date 哈希去重）
ANN = """title,time,url,source,summary,code,setcode,name
关于分红的公告,2026-08-05 00:00:00,http://example.com/a.pdf,上交所,证券代码：603605,603605,1,珀莱雅
董事会决议公告,2026-08-06 00:00:00,http://example.com/b.pdf,上交所,证券代码：603605,603605,1,珀莱雅
"""

# 估值快照 CSV
QUOTES = """code,setcode,name,snapshot_at,hqdate,hqtime,now,close,pe,pb,mgsy,mgjzc,zsz,zgb,ltgb,gdrs,ipoprice,zzc,jzc,jly,yysr,jyxjl
603605,1,珀莱雅,2026-08-07,20260807,110000,57.02,57.39,15.38,3.57,0.93,16.05,22554798100,39597.61,39597.61,73274,15.34,891212.625,640617.625,36668.67,230534.469,10697.11
"""

# 通达信 A 股利润表 CSV（ph_agf10_cw_lyb fixedTag=00101 年报；单位「元」原始）
# 字段顺序：code,setcode,period_end,fiscal_year,revenue,net_profit_attr,
#           eps_basic,eps_diluted,currency,unit,is_cumulative,published_at
FIN_A = """code,setcode,period_end,fiscal_year,revenue,net_profit_attr,eps_basic,eps_diluted,currency,unit,is_cumulative,published_at
600563,1,2025-12-31,2025,5326963978,1679653938,5.30,5.30,CNY,yuan,1,
"""

# 港股利润表 CSV（skef10_hk_cwfx fixedTag=1；单位「万元」，含公告日期）
FIN_HK = """code,setcode,period_end,fiscal_year,revenue,net_profit_attr,eps_basic,eps_diluted,currency,unit,is_cumulative,published_at
00700,31,2025-12-31,2025,6602570000,1650000000,17.50,17.50,CNY,万元,1,2026-03-18
"""


# ------------------------------------------------------------- K线 → daily_bars

def test_kline_a_stock_ingest_with_amount(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/kline/2026-08-07/run_t/603605.SH.csv", KLINE_A)
    r = ingest_file(conn, p, source="tdx", data_type="kline",
                    symbol="603605.SH", parse=tdx.parse_kline_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 3
    rows = conn.execute(
        "SELECT trade_date, open_raw, high_raw, low_raw, close_raw, "
        "volume_raw, amount_raw, currency, price_adj_factor FROM daily_bars "
        "WHERE symbol='603605.SH' ORDER BY trade_date"
    ).fetchall()
    assert len(rows) == 3
    assert rows[0]["trade_date"] == "2026-08-05"
    assert rows[0]["close_raw"] == 100.5
    # amount 非空（弥补 kimi 缺 amount 缺陷）
    assert rows[0]["amount_raw"] == 100500.0
    assert rows[0]["currency"] == "CNY"
    # 复权因子继承：新 bar 继承上一交易日；首日无历史落 1.0
    assert rows[0]["price_adj_factor"] == 1.0
    assert all(r["price_adj_factor"] == 1.0 for r in rows)
    # volume 单位换算：CSV volume=1000，unit 缺失默认 100（手）→ 100000 股
    assert rows[0]["volume_raw"] == 100000.0


def test_kline_hk_stock_with_missing_calendar(conn, tmp_path):
    # HK 日历未加 → incomplete 降级但仍入库（§2.5）
    p = write(tmp_path, "raw/tdx/kline/2026-08-07/run_t/00700.HK.csv", KLINE_HK)
    r = ingest_file(conn, p, source="tdx", data_type="kline",
                    symbol="00700.HK", parse=tdx.parse_kline_csv)
    assert r.status == "incomplete"
    assert any("trading_calendar 缺失" in x for x in r.incomplete_reasons)
    assert r.inserted == 1
    row = conn.execute(
        "SELECT market, currency, close_raw FROM daily_bars WHERE symbol='00700.HK'"
    ).fetchone()
    assert row["market"] == "HK"
    assert row["currency"] == "HKD"
    assert row["close_raw"] == 478.8


def test_kline_forward_file_skipped(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/kline/2026-08-07/run_t/603605.SH_tq1.csv", KLINE_FWD)
    r = ingest_file(conn, p, source="tdx", data_type="kline",
                    symbol="603605.SH", parse=tdx.parse_kline_csv)
    assert r.status == "ok"
    assert r.inserted == 0
    assert r.skipped == 1
    assert any("tqflag=1" in n for n in r.notes)
    assert _count(conn, "daily_bars") == 0


def test_kline_rejects_bad_ohlc(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/kline/2026-08-07/run_t/603605.SH.csv", KLINE_BAD_OHLC)
    r = ingest_file(conn, p, source="tdx", data_type="kline",
                    symbol="603605.SH", parse=tdx.parse_kline_csv)
    assert r.status == "conflict"
    assert "low<=open/close<=high" in r.errors[0]
    # 整批回滚
    assert _count(conn, "daily_bars") == 0


def test_kline_rejects_weekend_bar(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/kline/2026-08-07/run_t/603605.SH.csv", KLINE_WEEKEND)
    r = ingest_file(conn, p, source="tdx", data_type="kline",
                    symbol="603605.SH", parse=tdx.parse_kline_csv)
    assert r.status == "conflict"
    assert "非交易日" in r.errors[0]
    assert _count(conn, "daily_bars") == 0


# ------------------------------------------------------------- 指数 → index_bars

def test_index_ingest_setcode_62(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/index/2026-08-07/run_t/000300.SH.csv", INDEX_300)
    r = ingest_file(conn, p, source="tdx", data_type="index",
                    symbol="000300.SH", parse=tdx.parse_index_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 2
    rows = conn.execute(
        "SELECT index_code, trade_date, close, currency, volume FROM index_bars "
        "ORDER BY trade_date"
    ).fetchall()
    assert rows[0]["index_code"] == "000300.SH"  # 经 SETCODE_SUFFIX 归一
    assert rows[0]["trade_date"] == "2026-08-05"
    assert rows[0]["close"] == 4663.8
    assert rows[0]["currency"] == "CNY"
    # 指数 unit=1，volume 不被 ×100
    assert rows[0]["volume"] == 200000000


def test_index_rejects_non_index_setcode(conn, tmp_path):
    # setcode=1（个股）误入 index 目录 → 冲突
    bad = INDEX_300.replace("000300,62,", "603605,1,")
    p = write(tmp_path, "raw/tdx/index/2026-08-07/run_t/603605.SH.csv", bad)
    r = ingest_file(conn, p, source="tdx", data_type="index",
                    symbol="603605.SH", parse=tdx.parse_index_csv)
    assert r.status == "conflict"
    assert "不是指数" in r.errors[0]


# ------------------------------------------------------------- 公告 → events

def test_announcement_ingest_and_dedup(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/announcement/2026-08-07/run_t/603605.SH_p1.csv", ANN)
    r = ingest_file(conn, p, source="tdx", data_type="announcement",
                    symbol="603605.SH", parse=tdx.parse_announcement_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 2
    rows = conn.execute(
        "SELECT e.title, e.source, es.symbol FROM events e "
        "JOIN event_symbols es ON es.event_id = e.event_id "
        "ORDER BY e.title"
    ).fetchall()
    assert {r["title"] for r in rows} == {"关于分红的公告", "董事会决议公告"}
    assert all(r["symbol"] == "603605.SH" for r in rows)
    assert all(r["source"] == "tdx" for r in rows)
    # available_at 必须是下一个开市交易日（08-05 公告 → 08-06 00:00 本地）
    e1 = conn.execute(
        "SELECT available_at FROM events WHERE title='关于分红的公告'"
    ).fetchone()
    # 2026-08-06 00:00 Asia/Shanghai = 2026-08-05 16:00 UTC
    assert e1["available_at"].startswith("2026-08-05T16:00")


def test_announcement_idempotent(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/announcement/2026-08-07/run_t/603605.SH_p1.csv", ANN)
    ingest_file(conn, p, source="tdx", data_type="announcement",
                symbol="603605.SH", parse=tdx.parse_announcement_csv)
    # 同文件二次 ingest：content hash 命中 → skipped
    r2 = ingest_file(conn, p, source="tdx", data_type="announcement",
                     symbol="603605.SH", parse=tdx.parse_announcement_csv)
    assert r2.skipped == 1
    assert _count(conn, "events") == 2  # 不重复


def test_announcement_rejects_missing_ticker(conn, tmp_path):
    # 文件名无 ticker，CSV 行也无 code/setcode → 冲突
    no_code = "title,time,url,source,summary\n关于分红的公告,2026-08-05 00:00:00,http://x,上交所,无 code\n"
    p = write(tmp_path, "raw/tdx/announcement/2026-08-07/run_t/unknown.csv", no_code)
    r = ingest_file(conn, p, source="tdx", data_type="announcement",
                    symbol=None, parse=tdx.parse_announcement_csv)
    assert r.status == "conflict"
    assert "无法推断 ticker" in r.errors[0]


# ------------------------------------------------------------- 估值快照 → share_capital_events

def test_quotes_ingest_group_total_tdx(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/quotes/2026-08-07/run_t/603605.SH.csv", QUOTES)
    r = ingest_file(conn, p, source="tdx", data_type="quotes",
                    symbol="603605.SH", parse=tdx.parse_quotes_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 1
    row = conn.execute(
        "SELECT symbol, effective_at, event_type, share_count_type, "
        "shares_issued_after, details_json FROM share_capital_events "
        "WHERE symbol='603605.SH'"
    ).fetchone()
    assert row["event_type"] == "snapshot_group_total_tdx"
    assert row["share_count_type"] == "group_total_tdx"
    # zgb=39597.61 万股 → 395976100 股
    assert row["shares_issued_after"] == "395976100"
    import json
    details = json.loads(row["details_json"])
    assert details["pe"] == 15.38
    assert details["gdrs"] == 73274
    assert details["pb"] == 3.57


def test_quotes_idempotent(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/quotes/2026-08-07/run_t/603605.SH.csv", QUOTES)
    ingest_file(conn, p, source="tdx", data_type="quotes",
                symbol="603605.SH", parse=tdx.parse_quotes_csv)
    r2 = ingest_file(conn, p, source="tdx", data_type="quotes",
                     symbol="603605.SH", parse=tdx.parse_quotes_csv)
    assert r2.skipped == 1
    assert _count(conn, "share_capital_events", "WHERE event_type='snapshot_group_total_tdx'") == 1


def test_quotes_does_not_pollute_pe_take(conn, tmp_path):
    """tdx 估值快照 share_count_type=group_total_tdx 不参与 valuation.py PE 取数
    （valuation.py 只认 issued/group_total）。"""
    p = write(tmp_path, "raw/tdx/quotes/2026-08-07/run_t/603605.SH.csv", QUOTES)
    ingest_file(conn, p, source="tdx", data_type="quotes",
                symbol="603605.SH", parse=tdx.parse_quotes_csv)
    # 库内同 symbol 的 issued/group_total 快照应为 0（tdx 不污染）
    n = conn.execute(
        "SELECT COUNT(*) FROM share_capital_events "
        "WHERE symbol='603605.SH' AND share_count_type IN ('issued','group_total')"
    ).fetchone()[0]
    assert n == 0


# ------------------------------------------------------------- ingest CLI 路由

def test_ingest_cli_routes_tdx_kline(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/kline/2026-08-07/run_t/603605.SH.csv", KLINE_A)
    results, total = ingest_paths(conn, [str(p)])
    assert total.status == "ok"
    assert total.inserted == 3
    assert _count(conn, "daily_bars") == 3


def test_ingest_cli_routes_tdx_index(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/index/2026-08-07/run_t/000300.SH.csv", INDEX_300)
    results, total = ingest_paths(conn, [str(p)])
    assert total.status == "ok"
    assert total.inserted == 2
    assert _count(conn, "index_bars") == 2


def test_ingest_cli_routes_tdx_announcement(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/announcement/2026-08-07/run_t/603605.SH_p1.csv", ANN)
    results, total = ingest_paths(conn, [str(p)])
    assert total.status == "ok"
    assert total.inserted == 2
    assert _count(conn, "events") == 2


# ------------------------------------------------------------- 财报 → financial_reports/facts

def test_financials_a_stock_ingest_yuan(conn, tmp_path):
    """A 股年报：原始「元」入库（ph_agf10_cw_lyb fixedTag=00101）。"""
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_init/600563.SH_is_20251231.csv", FIN_A)
    r = ingest_file(conn, p, source="tdx", data_type="financials",
                    symbol="600563.SH", parse=tdx.parse_financials_csv)
    # A 股 entry 不返回 published_at → incomplete（记 degraded_available_at）
    assert r.inserted == 1
    assert r.incomplete_reasons, r.summary()
    row = conn.execute(
        "SELECT r.symbol, r.period_end, r.period_type, r.fiscal_year, "
        "r.currency, r.unit, r.is_cumulative, r.revision, r.published_at, "
        "f.revenue, f.net_profit_attr, f.eps_basic "
        "FROM financial_reports r JOIN financial_facts f ON f.report_id = r.report_id "
        "WHERE r.symbol='600563.SH'"
    ).fetchone()
    assert row["period_end"] == "2025-12-31"
    assert row["period_type"] == "annual"
    assert row["fiscal_year"] == 2025
    assert row["currency"] == "CNY"
    assert row["unit"] == "yuan"
    assert row["is_cumulative"] == 1
    assert row["revision"] == 1
    assert row["published_at"] is None   # A 股接口原样无此字段
    # revenue=5,326,963,978 元（原 CSV 写的就是元）；EPS=5.30 元
    assert row["revenue"] == "5326963978"
    assert row["net_profit_attr"] == "1679653938"
    assert row["eps_basic"] == "5.30"


def test_financials_hk_unit_convert_wan_yuan(conn, tmp_path):
    """港股利润表：原始「万元」→ /1e4 转「元」入库；published_at 走港股 entry 自带「公告日期」。"""
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_init/00700.HK_is_20251231.csv", FIN_HK)
    r = ingest_file(conn, p, source="tdx", data_type="financials",
                    symbol="00700.HK", parse=tdx.parse_financials_csv)
    assert r.inserted == 1, r.summary()
    row = conn.execute(
        "SELECT r.symbol, r.period_end, r.currency, r.unit, r.published_at, r.published_tz, "
        "f.revenue, f.net_profit_attr, f.eps_basic "
        "FROM financial_reports r JOIN financial_facts f ON f.report_id = r.report_id "
        "WHERE r.symbol='00700.HK'"
    ).fetchone()
    # 6602570000 万元 → 66,025,700,000,000 元（6.6 万亿，符合腾讯规模）
    assert row["revenue"] == "66025700000000"
    assert row["net_profit_attr"] == "16500000000000"
    assert row["eps_basic"] == "17.50"
    assert row["currency"] == "CNY"
    assert row["unit"] == "yuan"
    # 港股 entry 带「公告日期」2026-03-18 → published_at 非空
    assert row["published_at"] is not None
    assert row["published_at"].startswith("2026-03-17") or row["published_at"].startswith("2026-03-18")
    assert row["published_tz"] in ("Asia/Hong_Kong", "Asia/Shanghai")


def test_financials_revision_upgrade(conn, tmp_path):
    """同一报告期内容变化 → 新增 revision 不覆盖旧版（§3.7）。"""
    p1 = write(tmp_path, "raw/tdx/financials/2026-08-23/run_a/600563.SH_is_20251231.csv", FIN_A)
    ingest_file(conn, p1, source="tdx", data_type="financials",
                symbol="600563.SH", parse=tdx.parse_financials_csv)
    # 更正版：营收 +1 亿
    fin_revised = FIN_A.replace("5326963978", "5426963978")
    p2 = write(tmp_path, "raw/tdx/financials/2026-08-23/run_b/600563.SH_is_20251231.csv", fin_revised)
    r2 = ingest_file(conn, p2, source="tdx", data_type="financials",
                     symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r2.inserted == 1
    assert any("新增 revision=2" in n for n in r2.notes)
    rows = conn.execute(
        "SELECT revision, revenue FROM financial_reports r "
        "JOIN financial_facts f ON f.report_id = r.report_id "
        "WHERE r.symbol='600563.SH' AND r.period_end='2025-12-31' "
        "ORDER BY revision"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["revision"] == 1 and rows[0]["revenue"] == "5326963978"
    assert rows[1]["revision"] == 2 and rows[1]["revenue"] == "5426963978"


def test_financials_same_content_skipped(conn, tmp_path):
    """同一报告期同内容 → 跳过（content hash 去重 + 内容比对）。"""
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_a/600563.SH_is_20251231.csv", FIN_A)
    r1 = ingest_file(conn, p, source="tdx", data_type="financials",
                     symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r1.inserted == 1
    r2 = ingest_file(conn, p, source="tdx", data_type="financials",
                     symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r2.skipped == 1


def test_financials_published_at_backfill_on_same_content(conn, tmp_path):
    """内容一致但已有报告 published_at 为 NULL、本批带来披露日 → 回填不新增 revision。"""
    p1 = write(tmp_path, "raw/tdx/financials/2026-08-23/run_a/600563.SH_is_20251231.csv", FIN_A)
    r1 = ingest_file(conn, p1, source="tdx", data_type="financials",
                     symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r1.inserted == 1
    fin_with_pub = FIN_A.replace(
        "yuan,1,\n", "yuan,1,2026-08-05\n")
    assert fin_with_pub != FIN_A
    p2 = write(tmp_path, "raw/akshare/financials/2026-08-26/run_ak/600563.SH_is_20251231.csv",
               fin_with_pub)
    r2 = ingest_file(conn, p2, source="akshare", data_type="financials",
                     symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r2.updated == 1
    assert r2.inserted == 0 and r2.skipped == 0
    row = conn.execute(
        "SELECT revision, published_at, published_tz, available_at "
        "FROM financial_reports WHERE symbol='600563.SH' AND period_end='2025-12-31'"
    ).fetchone()
    assert row["revision"] == 1                      # 事实未变，不新增 revision
    assert row["published_at"] is not None           # NULL → 已回填
    assert row["published_tz"] == "Asia/Shanghai"
    assert row["available_at"] is not None
    rev = conn.execute(
        "SELECT reason, run_id FROM data_revisions "
        "WHERE table_name='financial_reports' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert "披露日回填" in rev["reason"]
    assert rev["run_id"] == "run_ak"                 # 采集批次目录名
    # 再次入库同一披露日：published_at 已非 NULL → 普通跳过，不重复回填
    r3 = ingest_file(conn, p2, source="akshare", data_type="financials",
                     symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r3.skipped == 1 and r3.updated == 0


def test_financials_period_end_fallback_from_csv(conn, tmp_path):
    """文件名缺 `_is_YYYYMMDD` → 回退 CSV period_end 列入库（2026-08-23 起不再是 conflict）。"""
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_a/600563.SH.csv", FIN_A)
    r = ingest_file(conn, p, source="tdx", data_type="financials",
                    symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r.inserted == 1, r.summary()
    assert any("period_end 列取值" in n for n in r.notes)
    row = conn.execute(
        "SELECT period_end, period_type FROM financial_reports WHERE symbol='600563.SH'"
    ).fetchone()
    assert row["period_end"] == "2025-12-31"
    assert row["period_type"] == "annual"


def test_financials_bad_filename_and_bad_period_end(conn, tmp_path):
    """文件名无 _is_ 且 CSV period_end 列缺失/非法 → conflict 不入库。"""
    bad = FIN_A.replace("2025-12-31", "")
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_a/600563.SH.csv", bad)
    r = ingest_file(conn, p, source="tdx", data_type="financials",
                    symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r.conflicts == 1
    assert any("period_end 列缺失/非法" in e for e in r.errors)


def test_financials_revision_run_id_is_batch_dir(conn, tmp_path):
    """record_revision 的 run_id 取采集批次目录名（与 yahoo_finance 约定一致），非 raw_object_id。"""
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_batch9/600563.SH_is_20251231.csv", FIN_A)
    r = ingest_file(conn, p, source="tdx", data_type="financials",
                    symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r.inserted == 1
    row = conn.execute(
        "SELECT run_id FROM data_revisions WHERE table_name='financial_reports'"
    ).fetchone()
    assert row["run_id"] == "run_batch9"


def test_financials_missing_code_setcode(conn, tmp_path):
    """缺 code/setcode → conflict。"""
    bad = """period_end,fiscal_year,revenue,net_profit_attr,eps_basic,eps_diluted,currency,unit,is_cumulative,published_at
2025-12-31,2025,5326963978,1679653938,5.30,5.30,CNY,yuan,1,
"""
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_a/600563.SH_is_20251231.csv", bad)
    r = ingest_file(conn, p, source="tdx", data_type="financials",
                    symbol="600563.SH", parse=tdx.parse_financials_csv)
    assert r.conflicts == 1


def test_ingest_cli_routes_tdx_financials(conn, tmp_path):
    """ingest CLI 按路径 → (tdx, financials) 路由。"""
    p = write(tmp_path, "raw/tdx/financials/2026-08-23/run_a/600563.SH_is_20251231.csv", FIN_A)
    results, total = ingest_paths(conn, [str(p)])
    assert total.inserted == 1
    assert _count(conn, "financial_reports") == 1
    assert _count(conn, "financial_facts") == 1


def test_ingest_cli_routes_tdx_quotes(conn, tmp_path):
    p = write(tmp_path, "raw/tdx/quotes/2026-08-07/run_t/603605.SH.csv", QUOTES)
    results, total = ingest_paths(conn, [str(p)])
    assert total.status == "ok"
    assert total.inserted == 1
    assert _count(conn, "share_capital_events") == 1
