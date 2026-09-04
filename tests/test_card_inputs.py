"""D3.3 排期卡底稿导出器测试（设计 §5.6、§3.2）。

锁定：
- 底稿 JSON 十段结构齐全（meta/earnings/forecasts/factor_snapshot/valuation_scale/
  market_snapshot/exhaustion_params/signal_status/daily_watch/config_params）；
- pe_ttm 分位数为线性插值口径，样本区间（首末日期）强制标注（§3.2）；
- 恐慌低点 pe_ttm 关联 = indicators_daily 当日值；
- TTM = 最新年报 + 本财年最新累计 − 上一财年同期累计（§3.7），同比序列正确；
- 一致预期裂口 = 券商 FY1 增速（百分数归一）− 最近季报实际增速；
- 衰竭阈值 = 下跌起点后前 4 周均量基数 × config 倍率（2.0 / 0.40 / 0.50 / 0.60）；
- 纯读取：导出前后库行数不变。
"""

from __future__ import annotations

import json

import pytest

from scripts.pipeline import db
from scripts.pipeline import card_inputs as ci
from scripts.signals.common import RULE_VERSION

SYM = "TEST.SH"

# 5 日 pe 序列，分位数可手算（线性插值）
PE_SERIES = {"2026-01-05": 10.0, "2026-01-06": 20.0, "2026-01-07": 30.0,
             "2026-01-08": 40.0, "2026-01-09": 50.0}
WEEKS = [("2026-01-02", 1000.0), ("2026-01-09", 100.0), ("2026-01-16", 200.0),
         ("2026-01-23", 300.0), ("2026-01-30", 400.0)]  # 后 4 周均量基数 250


def _make_db(path):
    """建 fixture 库，返回 (conn, path)。"""
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
    for we, vol in WEEKS:
        c.execute(
            """
            INSERT INTO weekly_bars (symbol, week_end_date, week_start_date,
                open_adj, high_adj, low_adj, close_adj, volume_adj, trading_days)
            VALUES (?, ?, ?, 10.0, 10.0, 10.0, 10.0, ?, 5)
            """,
            (SYM, we, we, vol),
        )
    c.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at,
            event_type, shares_issued_after, share_count_type, source, created_at)
        VALUES (?, '2026-01-01', ?, 'snapshot_issued', '100', 'issued', 'test', ?)
        """,
        (SYM, now, now),
    )
    # 财报：2025 年报 + 2025Q1 + 2026Q1（TTM = 100 + 25 − 20 = 105）
    for period_end, ptype, fy, rev, np_, eps in (
            ("2025-12-31", "annual", 2025, "1000", "100", "1.0"),
            ("2025-03-31", "quarterly", 2025, "200", "20", "0.2"),
            ("2026-03-31", "quarterly", 2026, "220", "25", "0.25")):
        cur = c.execute(
            """
            INSERT INTO financial_reports (symbol, period_end, period_type,
                fiscal_year, available_at, revision, currency, is_cumulative,
                ingested_at)
            VALUES (?, ?, ?, ?, ?, 1, 'CNY', 1, ?)
            """,
            (SYM, period_end, ptype, fy, now, now),
        )
        c.execute(
            """
            INSERT INTO financial_facts (report_id, revenue, net_profit_attr,
                eps_basic, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cur.lastrowid, rev, np_, eps, now),
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
    # 锚点：恐慌低点 2026-01-07（不复权 8.5），下跌起点 = 2026-01-02 周
    for atype, tdate, adj, raw, fb in (
            ("panic_low", "2026-01-07", 9.0, 8.5, 0),
            ("decline_start", "2026-01-02", 12.0, 11.5, 0)):
        c.execute(
            """
            INSERT INTO weekly_anchors (symbol, as_of, anchor_type, trade_date,
                adjusted_price, raw_price, is_fallback, run_id, created_at)
            VALUES (?, '2026-01-30', ?, ?, ?, ?, ?, 'test', ?)
            """,
            (SYM, atype, tdate, adj, raw, fb, now),
        )
    # 当前完成周五项衰竭信号：panic active、duration active，其余 inactive
    for sig, state, trig in (("panic", "active", 1), ("dry_up", "inactive", 0),
                             ("no_new_low_3w", "inactive", 0),
                             ("divergence", "inactive", 0),
                             ("duration", "active", 0)):
        c.execute(
            """
            INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
                triggered, details_json, run_id, rule_version, config_hash, created_at)
            VALUES (?, '2026-01-30', ?, ?, 1, ?, '{"reason": "condition_met"}',
                    'test', 'signals_v1', 'h', ?)
            """,
            (SYM, sig, state, trig, now),
        )
    c.commit()
    return c, path


@pytest.fixture()
def conn(tmp_path):
    c, _ = _make_db(tmp_path / "t.db")
    yield c
    c.close()


# ---------------------------------------------------------------- 十段结构

def test_ten_sections_complete(conn):
    doc = ci.build_inputs(conn, SYM)
    assert list(doc.keys()) == [
        "meta", "earnings", "forecasts", "factor_snapshot", "valuation_scale",
        "market_snapshot", "exhaustion_params", "signal_status", "daily_watch",
        "config_params"]
    m = doc["meta"]
    assert m["schema"] == "card_inputs_v2"
    assert m["data_cutoff"]["daily_bars"] == "2026-01-09"
    assert m["data_cutoff"]["financial_reports"] == "2026-03-31"
    e = doc["earnings"]
    assert {"reports", "ttm", "latest_report", "currency"} <= set(e)
    assert {"panic_lows", "pe_ttm_quantiles", "current_pe_ttm",
            "sample_window"} <= set(doc["valuation_scale"])
    assert {"trade_date", "close_raw", "pe_ttm", "shares_issued",
            "price_basis"} <= set(doc["market_snapshot"])
    assert {"anchor", "base", "thresholds", "prev_low_raw",
            "params_echo"} <= set(doc["exhaustion_params"])
    assert {"week_end_date", "active_count", "min_active_signals", "meets_min",
            "signals"} <= set(doc["signal_status"])
    assert {"as_of", "active_card", "facts"} <= set(doc["daily_watch"])
    fs = doc["factor_snapshot"]                        # fixture 无行业码 → 无映射
    assert fs["factors"] == [] and "无因子映射" in fs["note"]
    assert doc["config_params"]["rule_version"] == RULE_VERSION
    assert doc["config_params"]["config_hash"]
    assert doc["config_params"]["industry_factors_hash"]


# ---------------------------------------------------------------- 分位数与样本区间（§3.2）

def test_quantiles_linear_interpolation(conn):
    q = ci.build_inputs(conn, SYM)["valuation_scale"]["pe_ttm_quantiles"]
    # [10,20,30,40,50] 线性插值：p5=12 p25=20 p50=30 p75=40 p95=48
    assert q == {"p5": 12.0, "p25": 20.0, "p50": 30.0, "p75": 40.0, "p95": 48.0}
    assert ci.build_inputs(conn, SYM)["valuation_scale"]["current_pe_ttm"] == 50.0


def test_percentile_edge_cases():
    assert ci.percentile([], 50) is None
    assert ci.percentile([7.0], 95) == 7.0
    assert ci.percentile([0.0, 100.0], 25) == 25.0


def test_sample_window_annotation(conn):
    sw = ci.build_inputs(conn, SYM)["valuation_scale"]["sample_window"]
    assert sw["start"] == "2026-01-05" and sw["end"] == "2026-01-09"
    assert sw["n_days"] == 5 and sw["n_panic_lows"] == 1
    assert "§3.2" in sw["note"] and "样本" in sw["note"]


# ---------------------------------------------------------------- 恐慌低点 pe_ttm 关联

def test_panic_low_pe_join(conn):
    lows = ci.build_inputs(conn, SYM)["valuation_scale"]["panic_lows"]
    assert len(lows) == 1
    low = lows[0]
    assert low["trade_date"] == "2026-01-07"
    assert low["raw_price"] == 8.5           # 不复权（排期卡价区口径，§3.4）
    assert low["pe_ttm"] == 30.0            # = indicators_daily 2026-01-07 当日值
    assert low["is_fallback"] is False


# ---------------------------------------------------------------- 盈利底稿与一致预期

def test_earnings_ttm_and_yoy(conn):
    e = ci.build_inputs(conn, SYM)["earnings"]
    assert e["ttm"]["net_profit_attr"] == "105"      # 100 + 25 − 20（§3.7）
    assert e["ttm"]["eps"] == 1.05                    # 105 ÷ 股本 100
    assert e["ttm"]["shares"] == "100"
    latest = e["latest_report"]
    assert latest["period_end"] == "2026-03-31"
    assert latest["net_profit_yoy"] == pytest.approx(0.25)
    assert latest["revenue_yoy"] == pytest.approx(0.10)
    annual = next(r for r in e["reports"] if r["period_type"] == "annual")
    assert annual["net_profit_yoy"] is None           # 无 2024 年报，同比为空


def test_forecasts_gap_check(conn):
    f = ci.build_inputs(conn, SYM)["forecasts"]
    assert f["fy1_year"] == 2026
    assert f["net_profit"] == {"fy1": "130", "fy2": "150", "fy3": "165"}
    assert f["yoy_pct"]["fy1"] == pytest.approx(0.30)   # 百分数归一为分数
    assert f["yoy_pct"]["fy2"] == pytest.approx(150 / 130 - 1)
    gap = f["gap_check"]
    assert gap["actual_period"] == "2026-03-31"
    assert gap["actual_net_profit_yoy"] == pytest.approx(0.25)
    assert gap["gap_pp"] == pytest.approx(5.0)          # 30% − 25%（百分点）


# ---------------------------------------------------------------- 衰竭信号具体化参数

def test_exhaustion_thresholds(conn):
    x = ci.build_inputs(conn, SYM)["exhaustion_params"]
    assert x["prev_low_raw"] == 8.5                      # 当前锚点不复权前低
    assert x["base"]["decline_week"] == "2026-01-02"
    assert x["base"]["base_weeks"] == ["2026-01-09", "2026-01-16",
                                       "2026-01-23", "2026-01-30"]
    assert x["base"]["mean_volume_adj"] == 250.0         # (100+200+300+400)/4
    t = x["thresholds"]
    assert t["panic_volume_x2"] == 500.0                 # config vol_multiple 2.0
    assert t["dryup_volume_040"] == 100.0
    assert t["dryup_volume_050"] == 125.0                # config vol_ratio 0.50
    assert t["dryup_volume_060"] == 150.0


def test_signal_status_active_count(conn):
    s = ci.build_inputs(conn, SYM)["signal_status"]
    assert s["week_end_date"] == "2026-01-30"
    assert s["active_count"] == 2
    assert s["min_active_signals"] == 2
    assert s["meets_min"] is True
    assert set(s["active_signals"]) == {"panic", "duration"}
    assert len(s["signals"]) == 5


# ---------------------------------------------------------------- 导出文件与只读纪律

def test_export_writes_file_and_readonly(conn, tmp_path):
    counts_before = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("daily_bars", "indicators_daily", "weekly_bars",
                  "weekly_anchors", "signal_facts", "financial_reports",
                  "financial_facts", "forecasts", "strategy_card_versions")}
    doc, path = ci.export_inputs(conn, SYM, tmp_path / "cards")
    assert path.name == "inputs_2026-01-09.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["meta"]["schema"] == "card_inputs_v2"
    assert list(on_disk.keys()) == list(doc.keys())
    for t, n in counts_before.items():                   # 纯读取，不写库
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n


def test_unknown_symbol(conn):
    with pytest.raises(ci.CardInputsError, match="不在 watchlist"):
        ci.build_inputs(conn, "NOPE.SH")


def test_cli_summary(tmp_path, capsys):
    c, path = _make_db(tmp_path / "cli.db")
    c.close()
    rc = ci.main([SYM, "--db", str(path), "--out-dir", str(tmp_path / "out")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "底稿" in out and "PE(TTM) 50.0" in out and "样本" in out
    assert (tmp_path / "out" / "inputs_2026-01-09.json").exists()
