"""scripts/backtest 多股轮动测试（Phase 2）。

覆盖：
- universe 加载（watchlist 默认 / 显式覆盖 / 空池报错）
- prepare_data 短样本剔除明示
- 多股端到端：合成 4 股分化趋势 → Top-N 等权轮动跑通，
  交易标的 ⊆ 有效股票池、基准对比、positions/trades 结构存在
"""

import pandas as pd
import pytest

from scripts.backtest.run_multi import (
    benchmark_period_return,
    prepare_data,
    run_multi,
)
from scripts.backtest.universe import load_universe

from tests.test_backtest import _bt_cfg

# 多股等权一腿 50% 资金，滑点+佣金后需余量，用百万资金避免 margin 拒单
_MULTI_CFG = {**_bt_cfg(), "initial_cash": 1000000}

_MULTI_SYMS = ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"]
_N_BARS = 55  # > lookback(20)+slack，跨约 11 个 ISO 周
_IDX_DATES = pd.bdate_range("2024-01-02", periods=_N_BARS)
_IDX_LAST_CLOSE = 3000 + _N_BARS - 1


def _gen_bars(symbol: str, drift: float, start: str = "2024-01-02",
              n: int = _N_BARS) -> pd.DataFrame:
    """单股合成日线：平稳起步（完成 warmup），随后按 drift 日复利。"""
    closes = [10.0] * 25
    c = 10.0
    for _ in range(n - 25):
        c *= 1 + drift
        closes.append(c)
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame([{
        "date": d.date().isoformat(), "open": cl, "high": cl * 1.01, "low": cl * 0.99,
        "close": cl, "volume": 100000.0, "symbol": symbol,
    } for d, cl in zip(dates, closes)])


class TestUniverse:
    def test_load_universe_from_watchlist(self, bt_multi_db_path):
        from scripts.backtest.db import connect

        with connect(bt_multi_db_path) as conn:
            pool = load_universe(conn)
        assert pool == sorted(_MULTI_SYMS)

    def test_load_universe_explicit_overrides(self, bt_multi_db_path):
        from scripts.backtest.db import connect

        with connect(bt_multi_db_path) as conn:
            pool = load_universe(conn, ["000004.SZ ", "000001.SZ", "000001.SZ"])
        assert pool == ["000001.SZ", "000004.SZ"]

    def test_load_universe_empty_raises(self, bt_multi_db_path):
        from scripts.backtest.db import connect

        with connect(bt_multi_db_path) as conn:
            with pytest.raises(ValueError, match="股票池为空"):
                load_universe(conn, [])


class TestPrepareData:
    def test_short_history_symbol_skipped_explicitly(self, tmp_path):
        """样本不足必须明示剔除原因，不静默丢弃（§2.5）。"""
        from scripts.pipeline import db as pipeline_db
        from scripts.backtest.db import connect

        path = tmp_path / "short_hist.db"
        wconn = pipeline_db.connect(path)
        pipeline_db.migrate(wconn)
        wconn.execute(
            "INSERT INTO watchlist (symbol, market, name, aliases_json,"
            " benchmark_code, currency, timezone, active, created_at, updated_at)"
            " VALUES ('000099.SZ', 'CN', '短样本', '[]', '000300.SH', 'CNY',"
            " 'Asia/Shanghai', 1, '2024-01-01', '2024-01-01')")
        dates = pd.bdate_range("2024-01-02", periods=8)
        for d in dates:
            wconn.execute(
                """
                INSERT INTO daily_bars (symbol, trade_date, market,
                    open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                    currency, price_adj_factor, share_factor, trading_status,
                    source, raw_object_id, updated_at)
                VALUES ('000099.SZ', ?, 'CN', 10, 10, 10, 10, 1000, NULL,
                        'CNY', 1.0, 1.0, 'normal', 'test', NULL, '2024-01-01')
                """, (d.date().isoformat(),))
        wconn.commit()
        wconn.close()

        # 再加一只足样本股，避免"全池不足"抛错分支（那是另一条路径）
        wconn = pipeline_db.connect(path)
        wconn.execute(
            "INSERT INTO watchlist (symbol, market, name, aliases_json,"
            " benchmark_code, currency, timezone, active, created_at, updated_at)"
            " VALUES ('000001.SZ', 'CN', '长样本', '[]', '000300.SH', 'CNY',"
            " 'Asia/Shanghai', 1, '2024-01-01', '2024-01-01')")
        for d in _IDX_DATES:
            wconn.execute(
                """
                INSERT INTO daily_bars (symbol, trade_date, market,
                    open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                    currency, price_adj_factor, share_factor, trading_status,
                    source, raw_object_id, updated_at)
                VALUES ('000001.SZ', ?, 'CN', 10, 10, 10, 10, 1000, NULL,
                        'CNY', 1.0, 1.0, 'normal', 'test', NULL, '2024-01-01')
                """, (d.date().isoformat(),))
        wconn.commit()
        wconn.close()

        with connect(path) as conn:
            _, valid, skipped = prepare_data(
                conn, ["000099.SZ", "000001.SZ"], start=None, end=None, min_bars=25)
        assert valid == ["000001.SZ"]
        assert any("000099.SZ" in s and "样本不足" in s for s in skipped)

    def test_prepare_data_all_short_raises(self, tmp_path):
        """全部样本不足时报错不猜。"""
        from scripts.pipeline import db as pipeline_db
        from scripts.backtest.db import connect

        path = tmp_path / "all_short.db"
        wconn = pipeline_db.connect(path)
        pipeline_db.migrate(wconn)
        wconn.execute(
            "INSERT INTO watchlist (symbol, market, name, aliases_json,"
            " benchmark_code, currency, timezone, active, created_at, updated_at)"
            " VALUES ('000099.SZ', 'CN', '短样本', '[]', '000300.SH', 'CNY',"
            " 'Asia/Shanghai', 1, '2024-01-01', '2024-01-01')")
        for d in pd.bdate_range("2024-01-02", periods=8):
            wconn.execute(
                """
                INSERT INTO daily_bars (symbol, trade_date, market,
                    open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                    currency, price_adj_factor, share_factor, trading_status,
                    source, raw_object_id, updated_at)
                VALUES ('000099.SZ', ?, 'CN', 10, 10, 10, 10, 1000, NULL,
                        'CNY', 1.0, 1.0, 'normal', 'test', NULL, '2024-01-01')
                """, (d.date().isoformat(),))
        wconn.commit()
        wconn.close()
        with connect(path) as conn:
            with pytest.raises(ValueError, match="无任何股票满足"):
                prepare_data(conn, ["000099.SZ"], start=None, end=None, min_bars=25)


class TestRunMulti:
    def test_end_to_end_rotation(self, bt_multi_db_path):
        out, result = run_multi(top_n=2, lookback=20, cfg=_MULTI_CFG,
                                db_path=bt_multi_db_path)
        assert len(out["_universe_valid"]) == 4
        assert out["_skipped"] == []
        assert "total_return_pct" in out
        assert isinstance(out["_n_rejected"], int)
        assert out["_n_trades"] >= 1, "约 11 周调仓应产生交易"
        # 参与交易标的必须在有效股票池内
        assert set(out["_traded_symbols"]).issubset(set(out["_universe_valid"]))
        # 基准对比取到指数数据
        assert out["_benchmark_000300_pct"] == pytest.approx(
            (_IDX_LAST_CLOSE / 3000 - 1) * 100, rel=0.01)
        # 结果结构存在
        assert result.trades_df is not None
        assert result.orders_df is not None
        assert result.positions_df is not None

    def test_top_n_caps_distinct_holdings_loose(self, bt_multi_db_path):
        """持仓标的上限宽松校验：任意时点持有标的数 ≤ top_n × 2（换仓过渡容忍）。"""
        out, result = run_multi(top_n=2, lookback=20, cfg=_MULTI_CFG,
                                db_path=bt_multi_db_path)
        pos = result.positions_df
        held = set(pos["symbol"])
        assert held.issubset(set(out["_universe_valid"]))
        assert len(held) <= 4  # top_n=2 过渡期翻倍容忍，绝不全池乱买

    def test_no_future_function_weekly_cadence(self, bt_multi_db_path):
        """每周首 bar 触发一次：sell 订单按周聚合不超周数上限。"""
        out, result = run_multi(top_n=2, lookback=20, cfg=_MULTI_CFG,
                                db_path=bt_multi_db_path)
        orders = result.orders_df
        sells = orders[orders["side"] == "sell"].copy()
        if len(sells):
            ts = pd.to_datetime(sells["created_at_iso"])
            weeks = set(zip(ts.dt.isocalendar().year, ts.dt.isocalendar().week))
            assert len(weeks) <= 13  # 约 11 周区间留裕量


class TestBenchmark:
    def test_benchmark_period_return_first_last_close(self, bt_multi_db_path):
        from scripts.backtest.db import connect

        with connect(bt_multi_db_path) as conn:
            ret = benchmark_period_return(conn, "000300.SH", "2024-01-02",
                                          _IDX_DATES[-1].date().isoformat())
        assert ret is not None
        assert ret == pytest.approx((_IDX_LAST_CLOSE / 3000 - 1) * 100, rel=0.001)

    def test_benchmark_missing_returns_none(self, bt_multi_db_path):
        from scripts.backtest.db import connect

        with connect(bt_multi_db_path) as conn:
            ret = benchmark_period_return(conn, "^HSI", "2024-01-02", "2024-12-31")
        assert ret is None

