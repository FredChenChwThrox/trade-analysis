"""基本面分析底稿导出器测试（fundamental_inputs_v1，2026-09-03 新增）。

锁定：
- 底稿八段结构齐全；schema 标注正确；
- 派生指标公式：净利率/ROE（归母净利÷期末归母权益）/资产负债率/有息负债
  （四项求和，缺项按 0）/FCF（OCF−capex）/OCF 净利比；
- 缺 BS/CF 的期次带 incomplete 标记并计入 coverage/gaps（§2.5 不猜）；
- 隐含回报区间 = 一致预期 EPS × PE 历史分位（线性插值口径），upside 对现价；
- 毛利率取 THS 摘要快照并标 _ths 后缀；
- 纯读取：导出前后库行数不变。
"""

from __future__ import annotations

import json

import pytest

from scripts.pipeline import db
from scripts.pipeline import fundamental_inputs as fi

SYM = "TEST.SH"

PE_SERIES = {"2026-01-05": 10.0, "2026-01-06": 20.0, "2026-01-07": 30.0,
             "2026-01-08": 40.0, "2026-01-09": 50.0}


def _make_db(path):
    c = db.connect(path)
    db.migrate(c)
    now = db.utc_now()
    c.execute(
        """
        INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,
                               currency, timezone, active, created_at, updated_at)
        VALUES (?, 'CN', '测试', '[]', '000300.SH', 'CNY', 'Asia/Shanghai', 1, ?, ?)
        """,
        (SYM, now, now),
    )
    for d, pe in PE_SERIES.items():
        c.execute(
            """
            INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
                low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
                source, updated_at)
            VALUES (?, ?, 'CN', 10.0, 10.0, 10.0, 10.0, 1.0, 1.0, 1.0, 'test', ?)
            """,
            (SYM, d, now),
        )
        c.execute(
            """
            INSERT INTO indicators_daily (symbol, trade_date, pe_ttm, pe_status,
                computed_at)
            VALUES (?, ?, ?, 'ok', ?)
            """,
            (SYM, d, pe, now),
        )
    c.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at,
            event_type, shares_issued_after, share_count_type, source, created_at)
        VALUES (?, '2026-01-01', ?, 'snapshot_issued', '100', 'issued', 'test', ?)
        """,
        (SYM, now, now),
    )

    def report(period_end, ptype, fy, rev, np_, eps, *, bs=False, cf=False):
        cur = c.execute(
            """
            INSERT INTO financial_reports (symbol, period_end, period_type,
                fiscal_year, available_at, revision, currency, is_cumulative,
                ingested_at)
            VALUES (?, ?, ?, ?, ?, 1, 'CNY', 1, ?)
            """,
            (SYM, period_end, ptype, fy, now, now),
        )
        rid = cur.lastrowid
        c.execute(
            """
            INSERT INTO financial_facts (report_id, revenue, net_profit_attr,
                eps_basic, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rid, rev, np_, eps, now),
        )
        if bs:
            c.execute(
                """
                INSERT INTO balance_sheet_facts (report_id, total_assets,
                    total_liabilities, total_equity_attr, monetary_fund,
                    short_term_borrowing, long_term_borrowing, bonds_payable,
                    noncurrent_liab_1y, inventory, accounts_receivable,
                    accounts_payable, goodwill, updated_at)
                VALUES (?, '500', '200', '300', '100', '50', '30', NULL, NULL,
                        '20', '15', '25', '5', ?)
                """,
                (rid, now),
            )
        if cf:
            c.execute(
                """
                INSERT INTO cash_flow_facts (report_id, ocf, capex, icf,
                    financing_cf, net_cash_increase, updated_at)
                VALUES (?, '120', '40', '-30', '-10', '50', ?)
                """,
                (rid, now),
            )
        return rid

    # 2025 年报（BS+CF 齐）+ 2025Q1（缺 BS/CF）+ 2026Q1（BS+CF 齐）
    report("2025-12-31", "annual", 2025, "1000", "100", "1.0", bs=True, cf=True)
    report("2025-03-31", "quarterly", 2025, "200", "20", "0.2")
    report("2026-03-31", "quarterly", 2026, "220", "25", "0.25", bs=True, cf=True)
    c.execute(
        """
        INSERT INTO financial_indicator_snapshots (symbol, period_end, source,
            payload_json, ingested_at)
        VALUES (?, '2025-12-31', 'akshare', ?, ?)
        """,
        (SYM, json.dumps({"常用指标": {"归母净利润": "100"},
                          "盈利能力": {"毛利率": "35.5", "净资产收益率(ROE)": "33.3"}},
                         ensure_ascii=False), now),
    )
    c.execute(
        """
        INSERT INTO forecasts (symbol, snapshot_at, source, payload_json, ingested_at)
        VALUES (?, '2026-02-01T00:00:00+00:00', 'test', ?, ?)
        """,
        (SYM, json.dumps({"rows": [{
            "ths_fore_np_fy1_stock": "130", "ths_fore_np_fy2_stock": "150",
            "ths_fore_np_fy3_stock": "165", "ths_fore_np_yoy_stock": "30.0",
            "ths_fore_mbi_fy1_stock": "1300", "ths_fore_mbi_fy2_stock": "1500",
            "ths_fore_mbi_fy3_stock": "1650"}]}), now),
    )
    c.commit()
    return c, path


@pytest.fixture()
def conn(tmp_path):
    c, _ = _make_db(tmp_path / "t.db")
    yield c
    c.close()


# ---------------------------------------------------------------- 结构与派生指标

def test_eight_sections_complete(conn):
    doc = fi.build_inputs(conn, SYM)
    assert list(doc.keys()) == [
        "meta", "financials_multi_year", "forecasts", "valuation",
        "pool_comps", "events_summary", "factor_snapshot", "gaps"]
    assert doc["meta"]["schema"] == "fundamental_inputs_v1"
    assert {"panic_lows", "pe_ttm_quantiles", "implied_returns"} <= set(doc["valuation"])


def test_derived_metrics_formulas(conn):
    fin = fi.build_inputs(conn, SYM)["financials_multi_year"]
    annual = next(p for p in fin["periods"] if p["period_end"] == "2025-12-31")
    d = annual["derived"]
    assert d["net_margin"] == pytest.approx(0.1)          # 100/1000
    assert d["roe"] == pytest.approx(100 / 300)           # 归母净利÷期末归母权益
    assert d["asset_liability_ratio"] == pytest.approx(0.4)   # 200/500
    assert d["interest_bearing_debt"] == "80"             # 50+30（缺项按 0）
    assert d["fcf"] == "80"                               # 120−40
    assert d["ocf_to_np"] == pytest.approx(1.2)           # 120/100
    assert d["gross_margin_ths"] == "35.5"                # THS 摘要快照值
    assert d["roe_ths"] == "33.3"
    assert annual["incomplete"] == []                     # 全齐期次无 incomplete
    assert annual["revenue_yoy"] is None                  # 无 2024 年报


def test_missing_statement_marked_incomplete(conn):
    fin = fi.build_inputs(conn, SYM)["financials_multi_year"]
    q1_25 = next(p for p in fin["periods"] if p["period_end"] == "2025-03-31")
    assert "资产负债表缺失" in q1_25["incomplete"]
    assert "现金流量表缺失" in q1_25["incomplete"]
    assert "毛利率" in q1_25["incomplete"][2]
    d = q1_25["derived"]
    assert d["roe"] is None and d["fcf"] is None          # 缺输入 → None 不猜
    assert fin["coverage"] == {"n_periods": 3, "bs_missing_periods": 1,
                               "cf_missing_periods": 1}
    doc = fi.build_inputs(conn, SYM)
    assert any("1 个期次缺资产负债表" in g for g in doc["gaps"])


def test_yoy_across_years(conn):
    fin = fi.build_inputs(conn, SYM)["financials_multi_year"]
    q1_26 = next(p for p in fin["periods"] if p["period_end"] == "2026-03-31")
    assert q1_26["revenue_yoy"] == pytest.approx(0.10)    # 220/200−1
    assert q1_26["net_profit_yoy"] == pytest.approx(0.25)


# ---------------------------------------------------------------- 隐含回报区间表

def test_implied_returns_table(conn):
    ir = fi.build_inputs(conn, SYM)["valuation"]["implied_returns"]
    assert ir["status"] == "ok"
    assert ir["sample_window"]["start"] == "2026-01-05"   # 样本区间强制标注
    fy1 = next(r for r in ir["rows"] if r["fy"] == "fy1")
    assert fy1["eps_forecast"] == pytest.approx(1.3)      # 130 ÷ 股本 100
    # PE 分位（[10,20,30,40,50] 线性插值）：p25=20 p50=30 p75=40
    assert fy1["p25"]["implied_price"] == pytest.approx(26.0)   # 1.3×20
    assert fy1["p50"]["implied_price"] == pytest.approx(39.0)
    assert fy1["p75"]["implied_price"] == pytest.approx(52.0)
    assert fy1["p50"]["upside_vs_close"] == pytest.approx(2.9)  # 39/10−1
    fy2 = next(r for r in ir["rows"] if r["fy"] == "fy2")
    assert fy2["fy_year"] == 2027
    assert fy2["p50"]["implied_price"] == pytest.approx(45.0)   # 1.5×30


def test_implied_returns_no_forecast_incomplete(conn):
    conn.execute("DELETE FROM forecasts")
    conn.commit()
    ir = fi.build_inputs(conn, SYM)["valuation"]["implied_returns"]
    assert all(r.get("status", "").startswith("incomplete") for r in ir["rows"])


# ---------------------------------------------------------------- 池内对比 / 事件 / 只读

def test_pool_comps_self_marked(conn):
    comps = fi.build_inputs(conn, SYM)["pool_comps"]["stocks"]
    assert len(comps) == 1
    c0 = comps[0]
    assert c0["is_self"] is True
    assert c0["pe_ttm"] == 50.0
    assert c0["latest_period"] == "2026-03-31"
    assert c0["roe"] == pytest.approx(25 / 300, abs=1e-6)   # 最新含 BS 期次（_r6 六位口径）
    assert c0["asset_liability_ratio"] == pytest.approx(0.4)


def test_events_summary_structure(conn):
    ev = fi.build_inputs(conn, SYM)["events_summary"]
    assert ev["as_of"] == "2026-01-09"
    assert ev["recent_events"] == []
    assert ev["pending_unevaluated_count"] == 0
    assert ev["upcoming_calendar_30d"] == []


def test_export_writes_file_and_readonly(conn, tmp_path):
    tables = ("financial_reports", "financial_facts", "balance_sheet_facts",
              "cash_flow_facts", "financial_indicator_snapshots", "forecasts")
    counts_before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in tables}
    doc, path = fi.export_inputs(conn, SYM, tmp_path / "reports")
    assert path.name == "fundamental_inputs_2026-01-09.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["meta"]["schema"] == "fundamental_inputs_v1"
    for t, n in counts_before.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n


def test_unknown_symbol(conn):
    with pytest.raises(fi.FundamentalInputsError, match="不在 watchlist"):
        fi.build_inputs(conn, "NOPE.SH")
