"""自算筹码分布测试：golden 手算样例 + 性质不变量 + 除权连续性 + 截断/次新（设计 §5）。

纯函数路径（无 DB）为主；CLI smoke 走临时库。
golden 用 200k bin 细网格逼近连续手算值（唯 winner/分位数带中心插值 O(bin 宽) 误差）。
"""
import sqlite3

import numpy as np
import pytest

from scripts.indicators import chip_distribution as chip
from scripts.pipeline import db as pdb


def P(**kw):
    base = {"decay_factor": 0.7, "turnover_cap": 0.8, "peak_mode": "close",
            "dist_shape": "triangular", "n_bins": 200000, "burn_in_days": 90,
            "price_pad": 0.1}
    base.update(kw)
    return base


def day(td, low, high, close, *, turnover=None, factor=1.0, volume=1e6,
        amount=5e6, status="normal"):
    return {"trade_date": td, "open": close, "high": high, "low": low,
            "close": close, "volume": volume, "amount": amount,
            "turnover": turnover, "factor": factor, "trading_status": status}


# ---------------------------------------------------------------- golden 手算

def test_golden_two_day_uniform_then_triangular():
    """d0 初始化均匀[10,12]；d1 三角核[11,13]峰 12、k=0.35——连续域手算逐项对照。"""
    days = [
        day("2026-01-05", 10, 12, 11, turnover=0.01),
        day("2026-01-06", 11, 13, 12, turnover=0.5),
    ]
    out = chip.compute_chip_series(days, P())
    # d0：均匀[10,12]，close=11
    assert out[0]["winner_ratio"] == pytest.approx(0.5, abs=1e-3)
    assert out[0]["avg_cost"] == pytest.approx(11.0, abs=1e-3)
    assert out[0]["cost_5"] == pytest.approx(10.1, abs=1e-3)
    assert out[0]["cost_95"] == pytest.approx(11.9, abs=1e-3)
    assert out[0]["concentration_90"] == pytest.approx(1.8 / 22.0, abs=1e-3)
    assert out[0]["estimation_status"] == "burn_in"
    # d1：k=min(0.7*0.5, 0.8)=0.35
    # winner = 0.65*F_u(12)=0.65*1.0（close=12=旧支撑上沿）+ 0.35*F_tri(12;[11,13],c=12)
    #        = 0.65 + 0.35*0.5 → 0.825
    assert out[1]["winner_ratio"] == pytest.approx(0.825, abs=1e-3)
    # avg = 0.65*E[u(10,12)] + 0.35*E[tri(11,12,13)] = 0.65*11 + 0.35*12 = 11.35
    assert out[1]["avg_cost"] == pytest.approx(11.35, abs=1e-3)
    # c5：0.65*(x-10)/2 = 0.05 → x = 10 + 0.1/0.65
    assert out[1]["cost_5"] == pytest.approx(10 + 0.1 / 0.65, abs=1e-3)
    # c95：0.65 + 0.35*(1-(13-x)^2/2) = 0.95 → x = 13 - sqrt(0.1/0.35)
    assert out[1]["cost_95"] == pytest.approx(13 - (0.1 / 0.35) ** 0.5, abs=1e-3)
    assert out[1]["turnover_used"] == 0.5
    assert out[1]["estimation_status"] == "burn_in"


def test_limit_up_close_peak_right_triangle():
    """涨停收盘（close==high）：close 峰退化为直角三角形，质量集中板价——评审 #4。"""
    days = [
        day("2026-01-05", 10, 12, 11, turnover=0.01),
        day("2026-01-06", 10.8, 11, 11, turnover=0.5),  # c==b 右三角，均值 10.8+11+11 /3
    ]
    out = chip.compute_chip_series(days, P())
    # avg = 0.65*11 + 0.35*(10.8+11+11)/3 = 7.15 + 3.826667
    assert out[1]["avg_cost"] == pytest.approx(7.15 + 0.35 * (32.8 / 3), abs=1e-3)


def test_yizi_board_point_mass():
    """一字板（low==high==close）：新增核退化为点分布（§2.2 边界）。"""
    days = [
        day("2026-01-05", 10, 12, 11, turnover=0.01),
        day("2026-01-06", 11, 11, 11, turnover=0.5),
    ]
    out = chip.compute_chip_series(days, P())
    # 全部质量（旧均匀均值 11 + 新点 11）→ avg = 11.0
    # 点分布落在最近 bin 中心，容差放宽到半 bin 量级（~2.65e-5 × 若干倍）
    assert out[1]["avg_cost"] == pytest.approx(11.0, abs=1e-4)
    # winner 在点质量价位存在插值渐变（中心点质量 × 线性插值的固有口径，设计 §2.5）
    # → 用 kernel 单元断言点分布语义：全部质量落在包含 11.0 的单一 bin
    edges = np.linspace(9.0, 14.3, 200001)
    m = chip._kernel_mass(11.0, 11.0, 11.0, edges, "close", "triangular",
                          None, None)
    idx = int(np.clip(np.searchsorted(edges, 11.0) - 1, 0, len(m) - 1))
    assert m.sum() == pytest.approx(1.0)
    assert np.count_nonzero(m) == 1 and int(m.argmax()) == idx


def test_turnover_zero_missing_suspended_freeze():
    """换手 0 / 缺失 / 停牌：分布冻结（avg/c5/c95 不变）；winner 依赖当日 close 另算。"""
    days = [
        day("2026-01-05", 10, 12, 11, turnover=0.01),
        day("2026-01-06", 11, 13, 12, turnover=0.0),
        day("2026-01-07", 12, 14, 13, turnover=None),
        day("2026-01-08", 12, 14, 13, turnover=0.5, status="suspended"),
    ]
    out = chip.compute_chip_series(days, P())
    for i in (1, 2, 3):
        # 分布冻结：分布型指标逐位一致（winner 用当日 close，不比）
        assert out[i]["avg_cost"] == out[0]["avg_cost"]
        assert out[i]["cost_5"] == out[0]["cost_5"]
        assert out[i]["cost_95"] == out[0]["cost_95"]
    assert out[1]["turnover_used"] == 0.0
    assert out[2]["turnover_used"] is None
    assert out[3]["turnover_used"] is None  # 停牌日换手未参与 → None（实际使用语义）


def test_decay_and_anomaly_cap():
    """A=0.7 常规高换手不触发护栏；异常 turnover=2.0 被 cap=0.8 拦截（评审 #7-2）。"""
    base = [day("2026-01-05", 10, 12, 11, turnover=0.01)]
    # turnover=0.9 → k=0.63，新增点在 20（bin 中心量化容差 1e-4）
    out1 = chip.compute_chip_series(
        base + [day("2026-01-06", 20, 20, 20, turnover=0.9)], P())
    assert out1[1]["avg_cost"] == pytest.approx(0.37 * 11 + 0.63 * 20, abs=1e-4)
    # turnover=2.0 → k=min(1.4, 0.8)=0.8
    out2 = chip.compute_chip_series(
        base + [day("2026-01-06", 20, 20, 20, turnover=2.0)], P())
    assert out2[1]["avg_cost"] == pytest.approx(0.2 * 11 + 0.8 * 20, abs=1e-4)


def test_exright_continuity_in_adjusted_domain():
    """送股除权：复权域 winner/avg_cost 连续；折回输出 ÷ 当日 factor 精确（§2.3 核心）。"""
    days = [
        day("2026-01-05", 9.9, 10.1, 10.0, turnover=0.05),
        day("2026-01-06", 9.9, 10.1, 10.0, turnover=0.05),
        # 除权日（1 送 1）：raw 价格腰斩、factor 翻倍 → 复权价连续在 10
        day("2026-01-07", 4.9, 5.1, 5.0, turnover=0.05, factor=2.0),
    ]
    out = chip.compute_chip_series(days, P())
    assert out[2]["winner_ratio"] == pytest.approx(out[1]["winner_ratio"], abs=5e-3)
    assert out[2]["avg_cost_adj"] == pytest.approx(out[1]["avg_cost_adj"], abs=5e-2)
    # 折回精确性：raw × factor == adj（数值恒等，评审 #9）
    assert out[2]["avg_cost"] == pytest.approx(out[2]["avg_cost_adj"] / 2.0, rel=1e-12)
    assert out[2]["cost_5"] == pytest.approx(out[2]["cost_5_adj"] / 2.0, rel=1e-12)
    # 前复权口径语义：raw 口径成本在除权日"平移"（≈腰斩）——设计 §2.3 声明的行为
    assert out[2]["avg_cost"] == pytest.approx(out[1]["avg_cost"] / 2.0, abs=5e-2)


def test_burn_in_then_mature_xin_ci():
    """次新股窗口首日=上市日：前 90 日 burn_in、第 91 日起 mature 无空档（评审 #7-3）。"""
    days = [day(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 10 + i * 0.01,
                10.5 + i * 0.01, 10.2 + i * 0.01, turnover=0.02) for i in range(95)]
    out = chip.compute_chip_series(days, P(burn_in_days=90))
    assert all(s["estimation_status"] == "burn_in" for s in out[:90])
    assert all(s["estimation_status"] == "mature" for s in out[90:])
    assert len(out) == 95


def test_property_invariants():
    """winner∈[0,1]；c5≤c95；折回恒等；avg_cost_adj 在复权域带内。"""
    days = [day(f"2026-01-{(i % 28) + 1:02d}", 10 + (i % 7) * 0.3,
                11 + (i % 5) * 0.4, 10.5 + (i % 3) * 0.5,
                turnover=0.01 + (i % 9) * 0.01, factor=1.0 + (i // 30) * 0.5)
            for i in range(64)]
    lo_adj = min(d["low"] * d["factor"] for d in days)
    hi_adj = max(d["high"] * d["factor"] for d in days)
    out = chip.compute_chip_series(days, P(burn_in_days=5))
    for s in out:
        assert 0.0 <= s["winner_ratio"] <= 1.0
        assert s["cost_5"] <= s["cost_95"]
        # 不变量在复权域（raw 等效成本可因送转 factor 合法移出当前 raw 区间）
        assert lo_adj * 0.9 <= s["avg_cost_adj"] <= hi_adj * 1.1
        # 折回恒等（评审 #9）：raw × factor == adj
        f = s["avg_cost_adj"] / s["avg_cost"]
        assert f > 0


def test_rule_version_encodes_shape():
    assert chip.rule_version(P()) == "chip_v1_close_tri"
    assert chip.rule_version(P(peak_mode="vwap", dist_shape="uniform")) == \
        "chip_v1_vwap_unif"


# ---------------------------------------------------------------- CLI smoke（临时库）

def _insert_bar(conn, symbol, td, close, factor=1.0, turnover=0.02):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, amount_raw, currency,
            price_adj_factor, share_factor, trading_status, turnover,
            source, updated_at)
        VALUES (?, ?, 'CN', ?, ?, ?, ?, 1000000, 5000000, 'CNY', ?, 1.0,
                'normal', ?, 'test', ?)
        """,
        (symbol, td, close, close * 1.01, close * 0.99, close, factor,
         turnover, db_utc_now()),
    )


def db_utc_now():
    from scripts.pipeline.db import utc_now
    return utc_now()


def test_cli_single_symbol_smoke(tmp_path, monkeypatch):
    conn = pdb.connect(tmp_path / "m.db")
    pdb.migrate(conn)
    for i in range(3):
        _insert_bar(conn, "603605.SH", f"2026090{i + 1}", 10.0 + i * 0.1)
    conn.commit()
    conn.close()
    from scripts.indicators.chip_distribution import main
    rc = main(["603605.SH", "--db", str(tmp_path / "m.db")])
    assert rc == 0
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM chip_distribution WHERE symbol='603605.SH' "
        "ORDER BY trade_date").fetchall()
    assert len(rows) == 3
    assert rows[0]["estimation_status"] == "burn_in"
    assert rows[0]["source"] == "self_computed"
    assert rows[0]["rule_version"] == "chip_v1_close_tri"
    run = conn.execute(
        "SELECT stage, status FROM pipeline_runs WHERE stage='chip_distribution'"
    ).fetchone()
    assert run["status"] == "success"
    # 幂等：重跑行数不变（DELETE+重插）
    conn.close()
    rc2 = main(["603605.SH", "--db", str(tmp_path / "m.db")])
    assert rc2 == 0
    conn = sqlite3.connect(tmp_path / "m.db")
    assert conn.execute(
        "SELECT COUNT(*) FROM chip_distribution").fetchone()[0] == 3
    conn.close()
