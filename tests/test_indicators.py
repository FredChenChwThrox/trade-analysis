"""D1.7 指标计算 golden tests（设计 §9.5 硬门槛：核心公式固定样本手工核算）。

锁定口径（与 scripts/indicators/core.py、valuation.py docstring 一致）：
- SMA/EMA(adjust=False) 初值与递推、MACD 柱=2*(DIF-DEA)、Wilder RSI（含全涨/全跌/走平
  边界与递推）、BOLL ddof=0、KDJ 初始 50 与零振幅沿用前值、窗口不足返回 NaN、
  历史均值 shift(1) 规则；
- TTM：年报直取、中报三件套、组成项缺失→空、点时可见性与修订版本；
- PE：股本缺失/TTM≤0/缺汇率→NULL+原因码，正常值手工核算；
- 集成：小样本全量重算（日线/周线行数、手工对照 ma5/pe、run 记录、重跑幂等）。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from scripts.indicators import core, valuation
from scripts.indicators.compute import recompute_indicators
from scripts.pipeline import db

# ---------------------------------------------------------------- core：均线/MACD

def test_sma_and_window_shortage():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    ma3 = core.sma(s, 3)
    assert ma3.iloc[:2].isna().all()          # 窗口不足返回 NaN，不用短窗口冒充
    assert ma3.iloc[2] == pytest.approx(2.0)
    assert ma3.iloc[4] == pytest.approx(4.0)
    assert core.sma(s, 250).isna().all()      # MA250 在 5 点样本上全空


def test_ema_adjust_false_recursion():
    """span=3 → α=0.5：e0=x0；e_t=0.5·x_t+0.5·e_{t-1}。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    e = core.ema(s, 3)
    assert list(e) == pytest.approx([1.0, 1.5, 2.25, 3.125])


def test_macd_hist_and_dif_definition():
    close = pd.Series([100.0 + (i % 7) for i in range(60)])
    dif, dea, hist = core.macd(close, 12, 26, 9)
    assert list(dif) == pytest.approx(list(core.ema(close, 12) - core.ema(close, 26)))
    assert list(dea) == pytest.approx(list(core.ema(dif, 9)))
    assert list(hist) == pytest.approx(list(2 * (dif - dea)))  # 柱 = 2*(DIF-DEA)


# ---------------------------------------------------------------- core：Wilder RSI

def test_rsi_wilder_seed_and_recursion():
    """window=6：首值=前 6 个 delta 的简单平均，其后 (avg×5+x)/6 递推。"""
    close = pd.Series([10.0, 12, 11, 13, 12, 14, 13, 15])
    r = core.rsi(close, 6)
    assert r.iloc[:6].isna().all()                       # 窗口不足
    # idx6：deltas +2,-1,+2,-1,+2,-1 → avg_gain=1.0, avg_loss=0.5, RS=2
    assert r.iloc[6] == pytest.approx(100 - 100 / 3)     # 66.6667
    # idx7：delta +2 → avg_gain=7/6, avg_loss=5/12, RS=2.8
    assert r.iloc[7] == pytest.approx(100 - 100 / 3.8)   # 73.6842


def test_rsi_boundary_all_up_all_down_flat():
    assert core.rsi(pd.Series([10.0, 11, 12, 13, 14, 15, 16]), 6).iloc[6] == 100.0
    assert core.rsi(pd.Series([16.0, 15, 14, 13, 12, 11, 10]), 6).iloc[6] == 0.0
    assert core.rsi(pd.Series([5.0] * 7), 6).iloc[6] == 50.0   # 走平：增益/损失皆 0


# ---------------------------------------------------------------- core：BOLL / KDJ / shift(1)

def test_boll_ddof0_and_bandwidth():
    close = pd.Series([1.0, 2.0, 3.0, 4.0])
    mid, upper, lower, bw = core.boll(close, 3, 2)
    std_pop = (2.0 / 3.0) ** 0.5                          # ddof=0 总体标准差
    assert mid.iloc[2] == pytest.approx(2.0)
    assert upper.iloc[2] == pytest.approx(2.0 + 2 * std_pop)
    assert lower.iloc[2] == pytest.approx(2.0 - 2 * std_pop)
    assert bw.iloc[2] == pytest.approx(2 * std_pop)       # (upper-lower)/mid
    assert mid.iloc[:2].isna().all()


def _trend_frame(n: int) -> pd.DataFrame:
    """close=1..n，high=close+1，low=close-1（用于 KDJ 手工核算）。"""
    close = pd.Series([float(i + 1) for i in range(n)])
    return pd.DataFrame({"high": close + 1, "low": close - 1, "close": close})


def test_kdj_initial_50_and_recursion():
    f = _trend_frame(10)
    k, d, j = core.kdj(f["high"], f["low"], f["close"], 9, 3, 3)
    assert k.iloc[:8].isna().all()                        # 窗口不足
    # idx8：HHV=10, LLV=0, RSV=(9-0)/10×100=90；K=50×2/3+90/3, D=50×2/3+K/3
    assert k.iloc[8] == pytest.approx(63.3333333)
    assert d.iloc[8] == pytest.approx(54.4444444)
    assert j.iloc[8] == pytest.approx(3 * 63.3333333 - 2 * 54.4444444)
    # idx9：HHV=11, LLV=1, RSV=90；K/D 递推
    assert k.iloc[9] == pytest.approx(72.2222222)
    assert d.iloc[9] == pytest.approx(60.3703704)


def test_kdj_zero_amplitude_carries_previous():
    """连续 9 根走平 bar（HHV=LLV）：RSV 无定义，K/D 沿用前值。"""
    f = _trend_frame(9)
    flat = pd.DataFrame({"high": [10.0] * 9, "low": [10.0] * 9, "close": [10.0] * 9})
    f = pd.concat([f, flat], ignore_index=True)
    k, d, _ = core.kdj(f["high"], f["low"], f["close"], 9, 3, 3)
    assert k.iloc[17] == pytest.approx(k.iloc[16])        # 零振幅沿用
    assert d.iloc[17] == pytest.approx(d.iloc[16])
    assert not pd.isna(k.iloc[17])


def test_historical_mean_shift1():
    """历史均值先 shift(1) 排除当前 bar：idx3 = mean(x0..x2)，不含 x3。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    hm = core.historical_mean(s, 3)
    assert hm.iloc[3] == pytest.approx(2.0)
    assert hm.iloc[:3].isna().all()


def test_pct_chg_and_amplitude_percent():
    frame = pd.DataFrame({
        "open": [10.0, 11.0], "high": [10.5, 12.0], "low": [9.5, 10.0],
        "close": [10.0, 11.0], "volume": [100.0, 200.0],
    })
    params = {"ma_windows": [5], "macd": {"fast": 12, "slow": 26, "signal": 9},
              "rsi_windows": [6], "boll": {"window": 20, "num_std": 2},
              "volume": {"ma_windows": [5], "stats_windows": [20]},
              "kdj": {"rsv_window": 9, "k_smooth": 3, "d_smooth": 3}}
    out = core.compute_indicators(frame, params)
    assert pd.isna(out["pct_chg"].iloc[0])
    assert out["pct_chg"].iloc[1] == pytest.approx(10.0)          # +10%（百分比存储）
    assert out["amplitude"].iloc[1] == pytest.approx((12.0 - 10.0) / 10.0 * 100)


# ---------------------------------------------------------------- valuation：TTM

def _rep(period_end, ptype, fy, np_, avail, rev=1, cum=True):
    return valuation.ReportView(
        period_end=period_end, period_type=ptype, fiscal_year=fy, is_cumulative=cum,
        net_profit_attr=Decimal(np_) if np_ is not None else None,
        available_at=avail, revision=rev, currency="CNY")


ANNUAL_24 = _rep("2024-12-31", "annual", 2024, "80", "2025-04-01T00:00:00+00:00")
Q1_24 = _rep("2024-03-31", "quarterly", 2024, "20", "2024-04-30T00:00:00+00:00")
Q1_25 = _rep("2025-03-31", "quarterly", 2025, "25", "2025-04-30T00:00:00+00:00")


def test_ttm_annual_direct():
    ttm, status = valuation.ttm_net_profit([ANNUAL_24])
    assert ttm == Decimal("80") and status == "ok"


def test_ttm_interim_three_piece():
    """中报/季报累计：TTM = 上年年报 + 本年累计 − 去年同期累计 = 80+25-20。"""
    ttm, status = valuation.ttm_net_profit([ANNUAL_24, Q1_24, Q1_25])
    assert ttm == Decimal("85") and status == "ok"


def test_ttm_missing_component_returns_none():
    ttm, status = valuation.ttm_net_profit([ANNUAL_24, Q1_25])   # 缺 2024Q1 同期
    assert ttm is None and status == valuation.S_MISSING_PREV_SAME
    ttm, status = valuation.ttm_net_profit([Q1_24, Q1_25])       # 缺上年年报
    assert ttm is None and status == valuation.S_MISSING_PREV_ANNUAL
    ttm, status = valuation.ttm_net_profit([])                   # 无可见报告
    assert ttm is None and status == valuation.S_NO_REPORT


def test_ttm_point_in_time_visibility():
    """as_of 早于季报披露 → 只用当时可见的年报。"""
    vis = valuation.visible_reports(
        [ANNUAL_24, Q1_25], datetime(2025, 4, 15, tzinfo=timezone.utc))
    ttm, _ = valuation.ttm_net_profit(vis)
    assert ttm == Decimal("80")
    vis = valuation.visible_reports(
        [ANNUAL_24, Q1_25], datetime(2025, 5, 1, tzinfo=timezone.utc))
    assert valuation.ttm_net_profit(vis)[0] is None              # 可见季报但缺三件套


def test_ttm_latest_revision_wins():
    rev2 = _rep("2024-12-31", "annual", 2024, "90", "2025-06-01T00:00:00+00:00", rev=2)
    vis = valuation.visible_reports(
        [ANNUAL_24, rev2], datetime(2025, 6, 2, tzinfo=timezone.utc))
    ttm, _ = valuation.ttm_net_profit(vis)
    assert ttm == Decimal("90")


# ---------------------------------------------------------------- valuation：PE

def test_pe_reason_codes():
    pe, st = valuation.pe_ttm(10.0, None, Decimal("500000000"), "ok")
    assert pe is None and st == valuation.S_NO_SHARE                       # 股本缺失
    pe, st = valuation.pe_ttm(10.0, Decimal("100000000"), Decimal("-1"), "ok")
    assert pe is None and st == valuation.S_TTM_NON_POSITIVE               # TTM<=0
    pe, st = valuation.pe_ttm(10.0, Decimal("100000000"), None, valuation.S_NO_REPORT)
    assert pe is None and st == valuation.S_NO_REPORT
    pe, st = valuation.pe_ttm(10.0, Decimal("100000000"), Decimal("500000000"), "ok",
                              fx_needed=True, fx_rate=None)
    assert pe is None and st == valuation.S_FX_MISSING                     # 缺汇率
    pe, st = valuation.pe_ttm(10.0, Decimal("100000000"), Decimal("500000000"), "ok")
    assert pe == pytest.approx(2.0) and st == "ok"                         # 10×1e8/5e8


def test_shares_at_effective_date():
    events = [valuation.ShareEventView("2023-08-09", "2026-08-09T00:00:00+00:00",
                                       Decimal("100"), "issued")]
    assert valuation.shares_at(events, "2023-08-08") is None
    assert valuation.shares_at(events, "2023-08-09") == Decimal("100")


# ---------------------------------------------------------------- 集成：小样本全量重算

def _weekdays(start: str, end: str) -> list[str]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "market.db")
    db.migrate(c)
    now = db.utc_now()
    c.execute(
        """
        INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,
                               currency, timezone, active, created_at, updated_at)
        VALUES ('TEST.SH', 'CN', '测试', '[]', '000300.SH', 'CNY', 'Asia/Shanghai', 1, ?, ?)
        """,
        (now, now),
    )
    for d in _weekdays("2026-07-01", "2026-08-31"):
        c.execute(
            """
            INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                                          session_open, session_close, status,
                                          status_detail, timezone, source, updated_at)
            VALUES ('CN', ?, 1, 1, NULL, NULL, 'trading', NULL, 'Asia/Shanghai', 'test', ?)
            """,
            (d, now),
        )
    days = _weekdays("2026-07-06", "2026-08-14")  # 30 个交易日
    for i, d in enumerate(days):
        p = 100.0 + 0.5 * i
        c.execute(
            """
            INSERT INTO daily_bars (symbol, trade_date, market,
                open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                currency, price_adj_factor, share_factor, trading_status,
                source, raw_object_id, updated_at)
            VALUES ('TEST.SH', ?, 'CN', ?, ?, ?, ?, ?, NULL, 'CNY', 1.0, 1.0,
                    'normal', 'test', NULL, ?)
            """,
            (d, p - 0.2, p + 0.7, p - 0.6, p, 1000.0 + 10 * i, now),
        )
    c.execute(
        """
        INSERT INTO weekly_bars (symbol, week_end_date, week_start_date,
            open_adj, high_adj, low_adj, close_adj, volume_adj, amount_raw,
            trading_days, run_id)
        VALUES ('TEST.SH', '2026-08-14', '2026-08-10', 1, 2, 0.5, 1.5, 5000, NULL, 5, 't')
        """,
    )
    c.execute(
        """
        INSERT INTO financial_reports (symbol, period_end, period_type, fiscal_year,
            published_at, published_tz, available_at, revision, currency, unit,
            is_cumulative, raw_object_id, ingested_at)
        VALUES ('TEST.SH', '2025-12-31', 'annual', 2025, NULL, NULL,
                '2026-08-09T12:00:00+00:00', 1, 'CNY', 'yuan', 1, NULL, ?)
        """,
        (now,),
    )
    c.execute(
        "INSERT INTO financial_facts (report_id, net_profit_attr, updated_at) "
        "VALUES (1, '1000000000', ?)",
        (now,),
    )
    c.execute(
        """
        INSERT INTO share_capital_events (symbol, effective_at, available_at, event_type,
            share_change, shares_issued_after, share_count_type, details_json, source,
            raw_object_id, created_at)
        VALUES ('TEST.SH', '2026-07-06', ?, 'snapshot_issued', NULL, '400000000',
                'issued', '{}', 'test', NULL, ?)
        """,
        (now, now),
    )
    c.commit()
    yield c
    c.close()


def test_recompute_full_sample(conn):
    with conn:
        res = recompute_indicators(conn, "TEST.SH", run_id="run_ind1")
    assert res.daily_rows == 30 and res.weekly_rows == 1
    assert res.pe_ok == 30 and res.pe_null == 0

    rows = {r["trade_date"]: r for r in conn.execute(
        "SELECT * FROM indicators_daily WHERE symbol = 'TEST.SH'")}
    last = rows["2026-08-14"]
    # ma5：p25..p29 = 112.5..114.5 → 113.5（因子 1.0，复权=不复权）
    assert last["ma5"] == pytest.approx(113.5)
    assert last["ma60"] is None                            # 窗口不足 → NULL
    # pe_ttm = close × 4e8 ÷ 1e9 = 114.5 × 0.4；年报直取
    assert last["pe_ttm"] == pytest.approx(114.5 * 0.4)
    assert last["pe_status"] == f"ok;{valuation.DEGRADED_AVAILABLE_AT}"
    assert last["dif"] is not None and last["dea"] is not None
    assert last["macd_hist"] == pytest.approx(2 * (last["dif"] - last["dea"]))
    assert last["run_id"] == "run_ind1"
    assert last["rule_version"] == core.RULE_VERSION
    assert last["config_hash"]

    wrow = conn.execute(
        "SELECT * FROM indicators_weekly WHERE symbol = 'TEST.SH'").fetchone()
    assert wrow["run_id"] == "run_ind1"

    run = conn.execute(
        "SELECT * FROM pipeline_runs WHERE run_id = 'run_ind1' AND stage = 'indicators'"
    ).fetchone()
    assert run["status"] == "success"
    assert run["rule_version"] == core.RULE_VERSION
    assert run["config_hash"] == res.config_hash
    assert "pandas" in run["app_version"]

    # 重跑幂等：DELETE+重插，行数与值一致
    with conn:
        res2 = recompute_indicators(conn, "TEST.SH", run_id="run_ind2")
    assert res2.daily_rows == 30 and res2.weekly_rows == 1
    row2 = conn.execute(
        "SELECT ma5, pe_ttm FROM indicators_daily WHERE symbol = 'TEST.SH' "
        "AND trade_date = '2026-08-14'").fetchone()
    assert row2["ma5"] == pytest.approx(113.5)
    assert row2["pe_ttm"] == pytest.approx(114.5 * 0.4)


def test_recompute_ttm_missing_marks_pe_null(conn):
    """删除年报只留季报三件套不全 → pe_ttm NULL + 原因码（§2.5，不冒充）。"""
    conn.execute("DELETE FROM financial_facts WHERE report_id = 1")
    conn.execute("DELETE FROM financial_reports WHERE symbol = 'TEST.SH'")
    now = db.utc_now()
    conn.execute(
        """
        INSERT INTO financial_reports (symbol, period_end, period_type, fiscal_year,
            published_at, published_tz, available_at, revision, currency, unit,
            is_cumulative, raw_object_id, ingested_at)
        VALUES ('TEST.SH', '2026-03-31', 'quarterly', 2026, NULL, NULL,
                '2026-08-09T12:00:00+00:00', 1, 'CNY', 'yuan', 1, NULL, ?)
        """,
        (now,),
    )
    conn.commit()
    with conn:
        res = recompute_indicators(conn, "TEST.SH", run_id="run_ind3")
    assert res.pe_ok == 0 and res.pe_null == 30
    row = conn.execute(
        "SELECT pe_ttm, pe_status FROM indicators_daily WHERE symbol = 'TEST.SH' "
        "AND trade_date = '2026-08-14'").fetchone()
    assert row["pe_ttm"] is None
    assert row["pe_status"].startswith(valuation.S_MISSING_PREV_ANNUAL)
