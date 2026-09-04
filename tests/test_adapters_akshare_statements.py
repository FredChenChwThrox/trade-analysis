"""akshare 基本面数据层 adapter 测试：sina 资产负债表/现金流量表 + THS 财务摘要。

覆盖：表头复用/新建降级、facts 挂接、幂等跳过、内容更正原地更新 + data_revisions、
income 对账（0.5% 容差）、摘要回填缺失 income facts、ingest 路由注册。
"""

import csv
import json

import pytest

from scripts.adapters import akshare as ak_adapter
from scripts.adapters.common import ingest_file
from scripts.pipeline import db as pdb


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(tmp_path / "market.db")
    pdb.migrate(c)
    pdb.seed(c)  # watchlist + CN 2023-2026 日历
    yield c
    c.close()


def _write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)


_BS_COLS = ["code", "setcode", "period_end", "fiscal_year", "is_cumulative",
            "currency", "unit", "total_assets", "total_liabilities",
            "total_equity_attr", "monetary_fund", "short_term_borrowing",
            "long_term_borrowing", "bonds_payable", "noncurrent_liab_1y",
            "inventory", "accounts_receivable", "accounts_payable", "goodwill"]

_CF_COLS = ["code", "setcode", "period_end", "fiscal_year", "is_cumulative",
            "currency", "unit", "ocf", "capex", "icf", "financing_cf",
            "net_cash_increase"]

_ABSTRACT_COLS = ["period_end", "group", "indicator", "value"]


def _bs_row(period_end, total_assets="10866569404.94", total_equity="6708330495.49",
            fiscal_year=None):
    return ["603605", "1", period_end, fiscal_year or period_end[:4], "1", "CNY",
            "yuan", total_assets, "3657943896.76", total_equity, "5372588109.51",
            "80048000.0", "", "822579500.49", "11644794.99", "853529366.36",
            "298056697.94", "1487953698.0", "923541784.87"]


def _seed_income_report(conn, period_end="2026-06-30", revenue="5374800725.36",
                        net_profit="1167896499.41", with_facts=True):
    """预置一条利润表表头（+facts），模拟 tdx/东财通道已入库的期次。"""
    cur = conn.execute(
        """
        INSERT INTO financial_reports (symbol, period_end, period_type, fiscal_year,
            published_at, published_tz, available_at, revision,
            currency, unit, is_cumulative, raw_object_id, ingested_at)
        VALUES ('603605.SH', ?, 'interim', 2026, '2026-08-24T16:00:00+00:00',
                'Asia/Shanghai', '2026-08-24T16:00:00+00:00', 1,
                'CNY', 'yuan', 1, NULL, '2026-08-25T00:00:00+00:00')
        """,
        (period_end,),
    )
    if with_facts:
        conn.execute(
            """
            INSERT INTO financial_facts (report_id, revenue, net_profit_attr,
                eps_basic, eps_diluted, shares_issued_end, shares_float_end,
                share_count_type, updated_at)
            VALUES (?, ?, ?, '2.94', NULL, NULL, NULL, NULL,
                    '2026-08-25T00:00:00+00:00')
            """,
            (cur.lastrowid, revenue, net_profit),
        )
    conn.commit()  # 预置行先落盘，避免被 ingest_file 事务回滚连带撤销
    return cur.lastrowid


# ---------------------------------------------------------------- balance_sheet

def test_balance_sheet_attach_to_existing_header(conn, tmp_path):
    """已有利润表表头的期次：BS facts 挂到同一 report_id，不新建表头。"""
    report_id = _seed_income_report(conn)
    path = tmp_path / "raw" / "akshare" / "balance_sheet" / "2026-09-03" / "run_b" / "603605.SH_bs.csv"
    _write_csv(path, _BS_COLS, [_bs_row("2026-06-30")])
    r = ingest_file(conn, path, source="akshare", data_type="balance_sheet",
                    symbol="603605.SH", parse=ak_adapter.parse_balance_sheet_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 1
    bs = conn.execute("SELECT * FROM balance_sheet_facts WHERE report_id=?",
                      (report_id,)).fetchone()
    assert bs["total_assets"] == "10866569404.94"
    assert bs["total_equity_attr"] == "6708330495.49"
    assert bs["long_term_borrowing"] is None  # 空字段 → NULL
    # 未新建表头、未动 income facts
    assert conn.execute("SELECT COUNT(*) FROM financial_reports").fetchone()[0] == 1
    assert conn.execute(
        "SELECT revenue FROM financial_facts WHERE report_id=?",
        (report_id,)).fetchone()[0] == "5374800725.36"


def test_balance_sheet_creates_degraded_header_for_old_period(conn, tmp_path):
    """无表头的历史期次：新建降级表头（published_at=NULL，available_at=入库时间）。"""
    path = tmp_path / "603605.SH_bs.csv"
    _write_csv(path, _BS_COLS, [_bs_row("2015-12-31", fiscal_year="2015")])
    r = ingest_file(conn, path, source="akshare", data_type="balance_sheet",
                    symbol="603605.SH", parse=ak_adapter.parse_balance_sheet_csv)
    assert r.status == "incomplete"  # 降级表头必须显式标注（§2.5）
    assert any("新建 1 个历史期财报表头" in x for x in r.incomplete_reasons)
    rep = conn.execute(
        "SELECT * FROM financial_reports WHERE symbol='603605.SH' "
        "AND period_end='2015-12-31'").fetchone()
    assert rep["period_type"] == "annual"
    assert rep["published_at"] is None
    assert rep["available_at"] is not None  # 入库时间降级
    assert rep["revision"] == 1
    bs = conn.execute("SELECT * FROM balance_sheet_facts WHERE report_id=?",
                      (rep["report_id"],)).fetchone()
    assert bs is not None


def test_balance_sheet_idempotent_and_revision_on_change(conn, tmp_path):
    """同内容重跑跳过；内容变化原地更新并记 data_revisions（不新增 header revision）。"""
    path = tmp_path / "603605.SH_bs.csv"
    _write_csv(path, _BS_COLS, [_bs_row("2015-12-31", fiscal_year="2015")])
    kw = dict(source="akshare", data_type="balance_sheet", symbol="603605.SH",
              parse=ak_adapter.parse_balance_sheet_csv)
    r1 = ingest_file(conn, path, **kw)
    assert r1.inserted == 1
    # 同字节文件被 content-hash 门槛拦截；改一个字节模拟同内容重解析
    r2 = ingest_file(conn, path, **kw)
    assert r2.skipped == 1 and r2.inserted == 0

    path2 = tmp_path / "603605.SH_bs_v2.csv"
    _write_csv(path2, _BS_COLS, [
        _bs_row("2015-12-31", total_assets="999", fiscal_year="2015")])
    r3 = ingest_file(conn, path2, **kw)
    assert r3.updated == 1
    bs = conn.execute(
        "SELECT b.total_assets FROM balance_sheet_facts b "
        "JOIN financial_reports r ON r.report_id=b.report_id "
        "WHERE r.period_end='2015-12-31'").fetchone()
    assert bs["total_assets"] == "999"
    rev = conn.execute(
        "SELECT * FROM data_revisions WHERE table_name='balance_sheet_facts'").fetchone()
    assert rev is not None
    assert conn.execute(
        "SELECT MAX(revision) FROM financial_reports WHERE symbol='603605.SH'"
    ).fetchone()[0] == 1  # header revision 不动


def test_balance_sheet_bad_period_conflict(conn, tmp_path):
    path = tmp_path / "603605.SH_bs.csv"
    _write_csv(path, _BS_COLS, [
        ["603605", "1", "2026/06/30", "2026", "1", "CNY", "yuan",
         "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"]])
    r = ingest_file(conn, path, source="akshare", data_type="balance_sheet",
                    symbol="603605.SH", parse=ak_adapter.parse_balance_sheet_csv)
    assert r.status == "conflict"
    assert conn.execute("SELECT COUNT(*) FROM balance_sheet_facts").fetchone()[0] == 0


# ---------------------------------------------------------------- cash_flow

def test_cash_flow_ingest(conn, tmp_path):
    report_id = _seed_income_report(conn)
    path = tmp_path / "603605.SH_cf.csv"
    _write_csv(path, _CF_COLS, [
        ["603605", "1", "2026-06-30", "2026", "1", "CNY", "yuan",
         "950841180.71", "66425450.92", "-697822418.1", "-138126730.33",
         "114137641.09"]])
    r = ingest_file(conn, path, source="akshare", data_type="cash_flow",
                    symbol="603605.SH", parse=ak_adapter.parse_cash_flow_csv)
    assert r.status == "ok", r.summary()
    cf = conn.execute("SELECT * FROM cash_flow_facts WHERE report_id=?",
                      (report_id,)).fetchone()
    assert cf["ocf"] == "950841180.71"
    assert cf["capex"] == "66425450.92"
    assert cf["icf"] == "-697822418.1"  # 负值保留符号


def test_cash_flow_empty_row_skipped(conn, tmp_path):
    """字段全空的期次行级跳过（sina 偶发残缺行，§2.5）。"""
    path = tmp_path / "603605.SH_cf.csv"
    _write_csv(path, _CF_COLS, [
        ["603605", "1", "2014-12-31", "2014", "1", "CNY", "yuan",
         "", "", "", "", ""],
        ["603605", "1", "2015-12-31", "2015", "1", "CNY", "yuan",
         "100", "50", "-30", "10", "20"]])
    r = ingest_file(conn, path, source="akshare", data_type="cash_flow",
                    symbol="603605.SH", parse=ak_adapter.parse_cash_flow_csv)
    assert r.inserted == 1 and r.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM cash_flow_facts").fetchone()[0] == 1


# ---------------------------------------------------------------- fin_abstract

def _abstract_rows(periods, np_value="1167896499.41", rev_value="5374800725.36"):
    rows = []
    for pe in periods:
        rows += [
            [pe, "常用指标", "归母净利润", np_value],
            [pe, "常用指标", "营业总收入", rev_value],
            [pe, "常用指标", "基本每股收益", "2.94"],
            [pe, "财务风险", "资产负债率", "33.66"],
        ]
    return rows


def test_fin_abstract_snapshot_and_crosscheck(conn, tmp_path):
    """快照入库 + 与存量 income facts 对账通过（同值不记 incomplete）。"""
    report_id = _seed_income_report(conn)
    path = tmp_path / "603605.SH_abstract.csv"
    _write_csv(path, _ABSTRACT_COLS, _abstract_rows(["2026-06-30"]))
    r = ingest_file(conn, path, source="akshare", data_type="fin_abstract",
                    symbol="603605.SH", parse=ak_adapter.parse_fin_abstract_csv)
    assert r.status == "ok", r.summary()
    assert r.inserted == 1
    snap = conn.execute(
        "SELECT * FROM financial_indicator_snapshots WHERE symbol='603605.SH'").fetchone()
    payload = json.loads(snap["payload_json"])
    assert payload["常用指标"]["归母净利润"] == "1167896499.41"
    assert payload["财务风险"]["资产负债率"] == "33.66"
    assert snap["source"] == "akshare"
    # 对账通过：income facts 不被覆盖
    assert conn.execute("SELECT eps_basic FROM financial_facts WHERE report_id=?",
                        (report_id,)).fetchone()[0] == "2.94"


def test_fin_abstract_crosscheck_mismatch_incomplete(conn, tmp_path):
    """对账超差（>0.5%）记 incomplete，不覆盖存量 facts。"""
    report_id = _seed_income_report(conn)
    path = tmp_path / "603605.SH_abstract.csv"
    _write_csv(path, _ABSTRACT_COLS, _abstract_rows(["2026-06-30"],
                                                    np_value="2000000000"))
    r = ingest_file(conn, path, source="akshare", data_type="fin_abstract",
                    symbol="603605.SH", parse=ak_adapter.parse_fin_abstract_csv)
    assert r.status == "incomplete"
    assert any("对账超差" in x for x in r.incomplete_reasons)
    assert conn.execute(
        "SELECT net_profit_attr FROM financial_facts WHERE report_id=?",
        (report_id,)).fetchone()[0] == "1167896499.41"  # 未被覆盖


def test_fin_abstract_backfills_missing_income(conn, tmp_path):
    """无表头的历史期次：新建降级表头 + 回填 income facts（revenue/np/eps）。"""
    path = tmp_path / "603605.SH_abstract.csv"
    _write_csv(path, _ABSTRACT_COLS, _abstract_rows(["2015-12-31"],
                                                    np_value="143749635.89",
                                                    rev_value="1645210435.65"))
    r = ingest_file(conn, path, source="akshare", data_type="fin_abstract",
                    symbol="603605.SH", parse=ak_adapter.parse_fin_abstract_csv)
    assert any("回填 1 期 income facts" in x for x in r.incomplete_reasons)
    row = conn.execute(
        """
        SELECT f.revenue, f.net_profit_attr, f.eps_basic, r.published_at
        FROM financial_reports r JOIN financial_facts f ON f.report_id=r.report_id
        WHERE r.symbol='603605.SH' AND r.period_end='2015-12-31'
        """).fetchone()
    assert row["revenue"] == "1645210435.65"
    assert row["net_profit_attr"] == "143749635.89"
    assert row["published_at"] is None  # 降级表头


def test_fin_abstract_backfill_only_null_columns(conn, tmp_path):
    """facts 行存在但关键字段全空：只补 NULL 列，不动已有列。"""
    report_id = _seed_income_report(conn, with_facts=False)
    conn.execute(
        "INSERT INTO financial_facts (report_id, revenue, net_profit_attr,"
        " eps_basic, eps_diluted, shares_issued_end, shares_float_end,"
        " share_count_type, updated_at)"
        " VALUES (?, NULL, NULL, NULL, '2.90', NULL, NULL, NULL,"
        " '2026-08-25T00:00:00+00:00')", (report_id,))
    conn.commit()
    path = tmp_path / "603605.SH_abstract.csv"
    _write_csv(path, _ABSTRACT_COLS, _abstract_rows(["2026-06-30"]))
    r = ingest_file(conn, path, source="akshare", data_type="fin_abstract",
                    symbol="603605.SH", parse=ak_adapter.parse_fin_abstract_csv)
    assert any("回填" in x for x in r.incomplete_reasons)
    row = conn.execute("SELECT * FROM financial_facts WHERE report_id=?",
                       (report_id,)).fetchone()
    assert row["revenue"] == "5374800725.36"   # NULL 列补上
    assert row["eps_diluted"] == "2.90"        # 已有列不动


def test_fin_abstract_bad_filename_conflict(conn, tmp_path):
    path = tmp_path / "abstract.csv"
    _write_csv(path, _ABSTRACT_COLS, _abstract_rows(["2026-06-30"]))
    r = ingest_file(conn, path, source="akshare", data_type="fin_abstract",
                    symbol=None, parse=ak_adapter.parse_fin_abstract_csv)
    assert r.status == "conflict"
    assert conn.execute(
        "SELECT COUNT(*) FROM financial_indicator_snapshots").fetchone()[0] == 0


# ---------------------------------------------------------------- 路由注册

def test_statement_routes_registered():
    from scripts.pipeline.ingest import _ROUTES

    assert _ROUTES[("akshare", "balance_sheet")] is ak_adapter.parse_balance_sheet_csv
    assert _ROUTES[("akshare", "cash_flow")] is ak_adapter.parse_cash_flow_csv
    assert _ROUTES[("akshare", "fin_abstract")] is ak_adapter.parse_fin_abstract_csv
