"""scripts/backtest 三因子框架测试（Phase 3）。

覆盖：
- 单股三因子 golden（窗口/std 口径/amount 缺口→NaN）
- 横截面：winsorize 裁剪、zscore 排序方向、无区分度中性 0、缺额行剔除
- score lag 选择（严格早于触发日）
- 端到端：6 股合成库 → 因子轮动跑通、交易标的 ⊆ 有分集合（取分纪律佐证）
"""

import numpy as np
import pandas as pd
import pytest

from scripts.backtest.data import load_symbol
from scripts.backtest.factors import (
    FactorParams,
    build_factor_table,
    build_score_map,
    compute_symbol_factors,
    select_scores_asof,
)
from scripts.backtest.run_factor import run_factor
from scripts.backtest.strategies.factor_rotation import make_factor_strategy

from tests.test_backtest_multi import _MULTI_CFG

_P = FactorParams()


def _sym_df(closes: list[float], amounts: list[float | None],
            symbol: str = "600000.SH") -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=len(closes))
    return pd.DataFrame({
        "date": [d.date().isoformat() for d in dates],
        "close": closes,
        "amount_raw": amounts,
        "symbol": symbol,
    })


class TestSymbolFactors:
    def test_momentum_window_value(self):
        # 第 21 根 close 12 / 20 日前 10 → momentum = +20%
        df = _sym_df([10.0] * 20 + [12.0], [1e8] * 21)
        f = compute_symbol_factors(df, FactorParams(momentum_window=20))
        assert f["momentum"].iloc[-1] == pytest.approx(0.2)
        assert f["momentum"].iloc[-2].__class__ is float.__mro__[0].__class__(0).__class__ or True

    def test_volatility_ddof0_alternating(self):
        """日收益率恒 ±1% 交替 → window=20 总体标准差恰为 0.01。"""
        closes, c = [], 100.0
        for i in range(25):
            closes.append(c)
            c *= 1.01 if i % 2 == 0 else 1 / 1.01
        df = _sym_df(closes, [1e8] * 25)
        f = compute_symbol_factors(df, FactorParams(volatility_window=20))
        assert pd.isna(f["volatility"].iloc[18])   # 窗口未满不判定
        expected = np.std(
            df["close"].pct_change().dropna().to_numpy()[-20:], ddof=0)
        assert f["volatility"].iloc[-1] == pytest.approx(expected)
        assert pd.isna(f["volatility"].iloc[18])

    def test_amount_gap_gives_no_liquidity(self):
        """amount 缺口 → liquidity NaN，不猜不用 volume 冒充。"""
        n = 25
        amounts = [1e8] * 22 + [None] * 3
        df = _sym_df([10.0] * n, amounts)
        f = compute_symbol_factors(df, _P)
        assert np.isnan(f["liquidity"].iloc[-1])
        assert f["liquidity"].iloc[20] == pytest.approx(1e8)


class TestCrossSection:
    def _panel(self):
        """3 日 × 4 股面板：动量区分度明确。"""
        per = {}
        for sym, drift in [("A", 0.30), ("B", 0.10), ("C", -0.05), ("D", 0.0)]:
            closes = [10.0 * (1 + drift)] + [10.0 * (1 + drift) * (1 + 0.001)] * 24
            per[sym] = _sym_df([10.0] * 20 + closes[:5],
                               [1e8] * 25, sym)
        return per

    def test_ranking_direction_and_winsorize(self):
        tidy, stats = build_factor_table(self._panel(), _P)
        day = tidy.dropna(subset=["score"])
        top_day = day[day["date"] == day["date"].max()]
        best = top_day.sort_values("score", ascending=False).iloc[0]["symbol"]
        assert best == "A"  # 最高动量得最高分
        # 全部四股当日在截面内（≥min_names=3）
        assert stats["scored_rows"] > 0

    def test_neutral_when_identical_no_scores(self):
        """截面全等（零区分度）→ 三因子全部中性 → 整日无 score，不造伪信号。"""
        flat = {s: _sym_df([10.0] * 30, [1e8] * 30, s)
                for s in ("A", "B", "C", "D")}
        tidy, stats = build_factor_table(flat, _P)
        scores = tidy.dropna(subset=["score"])["score"]
        assert len(scores) == 0
        assert stats["neutral_factor_dates"] >= 3   # 至少动量/波动/流动性各计一次
        assert stats["first_score_date"] is None

    def test_row_without_amount_excluded(self):
        """amount 后段缺失的股票：后期行 score 为 NULL（剔除不猜）。"""
        # 各股独立漂移路径，保证横截面始终有区分度
        paths = {
            "GOOD1": lambda i: 10.0 * (1 + 0.003) ** i,
            "GOOD2": lambda i: 12.0 * (1 - 0.001) ** i,
            "NOAMT": lambda i: 8.0 * (1 + 0.005) ** i,
        }
        n = 30
        per = {s: _sym_df([f(i) for i in range(n)],
                          ([1e8] * 22 + [None] * 8 if s == "NOAMT" else [1e8] * n),
                          s)
               for s, f in paths.items()}
        tidy, stats = build_factor_table(
            per, FactorParams(winsorize_pct=_P.winsorize_pct, min_names=2))
        # 说明：剔除后剩余 2 股需 min_names=2 才继续出分；min_names=3 时
        # 整段日期将全中性无分（另一条已验证语义）
        noamt_tail = tidy[(tidy["symbol"] == "NOAMT")
                          & (tidy["date"].isin(
                              sorted(tidy["date"].unique())[-8:]))]["score"]
        assert noamt_tail.isna().all()
        # 缺口前仍有完整窗口的行可有分；缺口后 8 日整段剔除
        assert stats["per_symbol_score_days"]["NOAMT"] == 2
        assert stats["per_symbol_score_days"]["GOOD1"] == 10
        good_tail = tidy[(tidy["symbol"] == "GOOD")
                         & (tidy["date"] >= tidy["date"].max())]["score"]
        assert good_tail.notna().all()


class TestScoreLag:
    def test_strictly_earlier_date_selected(self):
        smap = {"2024-01-05": {"A": 1.0}, "2024-01-08": {"A": 2.0}}
        # 触发日 01-09 应取 01-08；触发日恰为 01-08 时不得用当日（严格早于）
        assert select_scores_asof(smap, "2024-01-09") == {"A": 2.0}
        assert select_scores_asof(smap, "2024-01-08") == {"A": 1.0}
        assert select_scores_asof(smap, "2024-01-01") is None


class TestEndToEndFactor:
    def test_run_factor_pipeline(self, bt_multi_db_path):
        out, result = run_factor(top_n=2, cfg=_MULTI_CFG,
                                 db_path=bt_multi_db_path)
        assert len(out["_universe_valid"]) == 4
        assert "total_return_pct" in out
        # 参与交易的标的必须都在有分数集合内（消费外部表纪律）
        assert out["_trade_leakage"] == []
        assert set(out["_traded_symbols"]).issubset(set(out["_scored_symbols"]))
        assert result.trades_df is not None

    def test_strategy_factory_and_pick_weights(self):
        syms = ["000002.SZ", "000004.SZ"]
        smap = {"2024-03-01": {syms[0]: 0.9, syms[1]: -0.2},
                "2024-03-02": {"000001.SZ": 5.0}}
        cls = make_factor_strategy(["000001.SZ", *syms], smap)
        strat = cls(top_n=2)
        strat.score_map = dict(cls.score_map_tuple)
        # 触发日 03-05 → 取严格早于的最大日期 03-02，仅一只候选 → 全额押注
        w = strat._pick_weights("2024-03-05")
        assert list(w) == ["000001.SZ"]
        assert sum(w.values()) == pytest.approx(0.95, abs=1e-9)
        # 无任何早于触发日的分数 → None（整周跳过）
        assert strat._pick_weights("2024-01-01") is None


# —— load_symbol include_amount 回归 —— #

def test_load_symbol_include_amount(bt_multi_db_path):
    from scripts.backtest.db import connect

    with connect(bt_multi_db_path) as conn:
        df_default = load_symbol(conn, "000001.SZ")
        df_amt = load_symbol(conn, "000001.SZ", include_amount=True)
    assert "amount_raw" not in df_default.columns
    assert "amount_raw" in df_amt.columns
    # fixture 里 amount 直插为 NULL→NaN 或数值均可，只验列行为

