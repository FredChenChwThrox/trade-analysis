"""scripts/backtest 测试。

覆盖：
- data.py 复权导出 golden（后复权 = raw × factor，量 = raw ÷ share_factor）
- run.py 配置加载 / kwargs 组装
- 端到端：临时库合成数据 → akquant 双均线回测 → 指标存在 / T+1 拒单 / 费用可复算
"""

import pandas as pd
import pytest
from pathlib import Path

from akquant import run_backtest

from scripts.backtest import data as bdata
from scripts.backtest import db as bdb
from scripts.backtest import run as brun
from scripts.backtest.strategies.dual_ma import DualMAStrategy

# 合成行情：6 个交易日、因子 2.0 平台、share_factor 2.0，制造复权跳跃
_BARS = [
    # trade_date, open, high, low, close, volume, factor, share
    ("2024-01-02", 10.0, 10.5, 9.8, 10.2, 100000, 1.0, 1.0),
    ("2024-01-03", 10.2, 10.6, 10.0, 10.4, 110000, 1.0, 1.0),
    ("2024-01-04", 10.4, 10.8, 10.2, 10.6, 120000, 1.0, 1.0),
    ("2024-01-05", 10.6, 11.0, 10.4, 10.8, 130000, 1.0, 1.0),
    ("2024-01-08", 10.8, 11.2, 10.6, 11.0, 140000, 1.0, 1.0),
    ("2024-01-09", 11.0, 11.4, 10.8, 11.2, 150000, 1.0, 1.0),
    ("2024-01-10", 11.2, 11.6, 11.0, 11.4, 160000, 2.0, 2.0),
    ("2024-01-11", 22.0, 22.6, 21.8, 22.4, 180000, 2.0, 2.0),
]


@pytest.fixture()
def bt_db_path(tmp_path):
    """建库 + seed 日历 + 插入合成 bars 的临时 DB（回测只读）。"""
    from scripts.pipeline import db as pipeline_db

    path = tmp_path / "market.db"
    conn = pipeline_db.connect(path)
    pipeline_db.migrate(conn)
    conn.execute(
        "INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,"
        " currency, timezone, active, created_at, updated_at)"
        " VALUES ('000001.SZ', 'CN', '测试股', '[]', '000300.SH', 'CNY',"
        " 'Asia/Shanghai', 1, '2024-01-01', '2024-01-01')")
    # 2024 年 CN 日历种子（daily_bars 校验需要交易日历）
    calendar = Path(__file__).resolve().parents[1] / "config" / "calendar_cn_2024.yaml"
    pipeline_db.seed_calendar(conn, calendar)
    for b in _BARS:
        d, o, h, l, c, v, f, s = b
        conn.execute(
            """
            INSERT INTO daily_bars (symbol, trade_date, market,
                open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                currency, price_adj_factor, share_factor, trading_status,
                source, raw_object_id, updated_at)
                VALUES ('000001.SZ', ?, 'CN', ?, ?, ?, ?, ?, NULL, 'CNY', ?, ?, 'normal',
                        'test', NULL, '2024-01-01')
            """, (d, o, h, l, c, v, f, s))
    conn.commit()
    conn.close()
    return path


def _bt_connect(bt_db_path):
    import os

    os.environ["BACKTEST_DB"] = str(bt_db_path)
    return bdb.connect(bt_db_path)


def _bt_cfg() -> dict:
    return {"initial_cash": 100000, "lot_size": 100, "t_plus_one": True,
            "commission_rate": 0.0003, "stamp_tax_rate": 0.0005,
            "slippage": 0.0001}


# 制造"T+1 当日卖出被拒"的合成序列。
# 双均线数学：前期全平价 10 时，spike 日 P>10 即短上穿长（买入信号）；
# 次日收盘 C 满足 P+C<20 则短立即回落下穿长（卖出信号，与买入成交同日）→ 被 T+1 拒。
def _make_spike_df() -> pd.DataFrame:
    closes = [10.0] * 25            # 平稳段（完成 warmup）
    closes += [10.6]                # spike：次日短上穿长 → 买入（成交于下一开）
    closes += [9.3]                 # 急跌：短下穿长 → 卖出提交日=买入成交日 → 被拒
    closes += [9.2, 9.1, 9.0, 8.9, 8.8, 8.7, 8.6, 8.5, 8.4]  # 续跌让补发卖单成交
    dates = pd.bdate_range("2024-01-01", periods=len(closes))
    rows = [{
        "date": d, "open": c, "high": c * 1.01, "low": c * 0.99,
        "close": c, "volume": 100000.0, "symbol": "000001.SZ",
    } for d, c in zip(dates, closes)]
    return pd.DataFrame(rows).sort_values(["date", "symbol"])


class TestData:
    def test_load_symbol_adjusts_prices_and_volume(self, bt_db_path):
        with _bt_connect(bt_db_path) as conn:
            df = bdata.load_symbol(conn, "000001.SZ")
        assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "symbol"]
        assert len(df) == len(_BARS)
        # 后复权：factor 平台前 1.0，平台后 2.0
        assert df.iloc[0]["close"] == pytest.approx(10.2)
        assert df.iloc[-1]["close"] == pytest.approx(44.8)  # 22.4 × 2.0
        assert df.iloc[-1]["open"] == pytest.approx(44.0)
        # 调整量 = raw ÷ share_factor
        assert df.iloc[-1]["volume"] == pytest.approx(180000 / 2.0)
        assert df.iloc[0]["volume"] == pytest.approx(100000)
        assert (df["symbol"] == "000001.SZ").all()

    def test_load_symbol_range_filter(self, bt_db_path):
        with _bt_connect(bt_db_path) as conn:
            df = bdata.load_symbol(conn, "000001.SZ", start="2024-01-08", end="2024-01-10")
        assert list(df["date"]) == ["2024-01-08", "2024-01-09", "2024-01-10"]

    def test_load_symbol_missing_raises(self, bt_db_path):
        with _bt_connect(bt_db_path) as conn:
            with pytest.raises(ValueError):
                bdata.load_symbol(conn, "600000.SH")


class TestConfig:
    def test_load_config_defaults(self):
        cfg = brun.load_config()
        assert cfg["lot_size"] == 100
        assert cfg["t_plus_one"] is True
        assert cfg["commission_rate"] == 0.0003
        assert cfg["stamp_tax_rate"] == 0.0005
        assert cfg["slippage"] == 0.0001

    def test_build_kwargs_slippage_policy(self):
        kwargs = brun.build_kwargs({"slippage": 0.0001, "lot_size": 100})
        assert kwargs["slippage"] == {"type": "percent", "value": 0.0001}
        assert kwargs["lot_size"] == 100
        # 不存在的 key 不传入
        assert "timezone" not in kwargs


class TestEndToEnd:
    def test_dual_ma_runs_and_metrics_present(self, bt_db_path):
        out, result = brun.run("000001.SZ", _bt_cfg(), db_path=bt_db_path)
        assert out["_n_bars"] == len(_BARS)
        assert "total_return_pct" in out
        assert "sharpe_ratio" in out
        assert "max_drawdown_pct" in out
        assert result.trades_df is not None
        assert result.orders_df is not None

    def test_dual_ma_no_future_function(self, bt_db_path):
        """长窗样本不足时不下单：8 根 bar < long_window(20)，不应产生任何订单。"""
        out, result = brun.run("000001.SZ", _bt_cfg(), db_path=bt_db_path)
        orders = result.orders_df
        assert len(orders) == 0, "8 根 bar 不足 long_window(20)，不应产生交易"

    def test_commission_recalculable(self, bt_db_path):
        """费用可复算：trades.commission ≈ 买入额×佣金率 + 卖出额×(佣金率+印花税)。"""
        _, result = brun.run("000001.SZ", _bt_cfg(), db_path=bt_db_path)
        orders = result.orders_df
        for _, o in orders.iterrows():
            if o["status"] != "filled":
                continue
            rate = 0.0003 + (0.0005 if o["side"] == "sell" else 0.0)
            expected = o["filled_value"] * rate
            assert o["commission"] == pytest.approx(expected, rel=0.05), o["id"]

    def test_t_plus_one_rejects_same_day_sell(self):
        """T+1：买入成交当日提交的卖单被拒（Available 0），次一交易日成功。"""
        df2 = _make_spike_df()
        res = run_backtest(
            data=df2, strategy=DualMAStrategy, symbols="000001.SZ",
            initial_cash=1000000, lot_size=100, t_plus_one=True,
            commission_rate=0.0003, stamp_tax_rate=0.0005,
            slippage={"type": "percent", "value": 0.0001},
        )
        orders = res.orders_df
        rej_mask = orders["reject_reason"].fillna("").astype(str).str.len() > 5
        rej = orders[rej_mask]
        assert len(rej) >= 1
        assert all("available position" in (r or "") for r in rej["reject_reason"])
        # 被拒单之后有成功成交的卖单（次一交易日补上）
        assert "rejected" in set(orders["status"])
        assert "filled" in set(orders["status"])
