"""Phase A 时序事件回测测试（衰竭择时层忠实机械化）。

覆盖：
- load_exhaustion_counts golden（同周多锚取最大组、并列取小 id）
- load_decline_starts：is_fallback 过滤 + HFQ 止损换算 = adjusted×(1-stop_pct)
- ExhaustionTimingBase 单元：episode 锁（同锚只开一次）/ 锚推进即终结 /
  折扣门绑定（工厂类属性通道）
- 端到端最小合成：一笔完整买卖循环
"""

import pandas as pd
import pytest

from scripts.backtest.event_signals import (
    latest_week_before,
    load_completed_weeks,
    load_decline_starts,
    load_exhaustion_counts,
)
from scripts.backtest.strategies.exhaustion_timing import (
    make_exhaustion_strategy,
)


@pytest.fixture()
def ev_db(tmp_path):
    """单股合成事件库：2 个真实锚 + 1 个 fallback 锚 + 分周信号事实。"""
    from scripts.pipeline import db as pipeline_db

    path = tmp_path / "ev.db"
    conn = pipeline_db.connect(path)
    pipeline_db.migrate(conn)
    conn.execute(
        "INSERT INTO watchlist (symbol, market, name, aliases_json,"
        " benchmark_code, currency, timezone, active, created_at, updated_at)"
        " VALUES ('600000.SH','CN','T','[]','000300.SH','CNY',"
        " 'Asia/Shanghai',1,'2024-01-01','2024-01-01')")
    # 日历（2024）
    pipeline_db.seed_calendar(
        conn, pipeline_db.CONFIG_DIR / "calendar_cn_2024.yaml")

    def insert_bar(d, c, f):
        conn.execute(
            """INSERT INTO daily_bars (symbol, trade_date, market,
               open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
               currency, price_adj_factor, share_factor, trading_status,
               source, raw_object_id, updated_at)
               VALUES ('600000.SH', ?, 'CN', ?, ?, ?, ?, 1000, NULL,'CNY',
                       ?, 1.0, 'normal', 'test', NULL, '2024-01-01')""",
            (d, c * 0.99, c * 1.01, c * 0.99, c, f))

    for i in range(40):                       # 2024-01-02 起 40 个交易日（> warmup21）
        d = str((pd.Timestamp("2024-01-02")
                 + pd.offsets.BDay(i)).date())
        insert_bar(d, 10.0, 1.0)

    # 完成周两条（落在 warmup 之后，事件才可被策略看到）
    for we in ("2024-02-16", "2024-02-23"):
        conn.execute(
            """INSERT INTO weekly_bars (symbol, week_end_date, week_start_date,
               open_adj, high_adj, low_adj, close_adj, volume_adj, amount_raw,
               trading_days, run_id)
               VALUES ('600000.SH', ?, '2024-01-01', 10,10.5,9.8,10.4,
                       5000, NULL, 5, 't')""", (we,))

    # 锚点：id1 real / id2 real / id3 fallback
    for aid, td, raw, fb in ((1, "2024-01-08", 11.0, 0),
                             (2, "2024-01-09", 12.0, 0),
                             (3, "2024-01-10", 13.0, 1)):
        conn.execute(
            """INSERT INTO weekly_anchors (anchor_id, as_of, symbol, anchor_type,
               trade_date, adjusted_price, raw_price, is_fallback, created_at)
               VALUES (?, '2024-02-16', '600000.SH', 'decline_start',
                       ?, ?, ?, ?, '2024-01-05')""",
            (aid, td, raw * 1.0, raw, fb))

    # 信号事实：week1 锚1 下 panic+duration active=2 触发；
    #          同周另一组 锚9 仅 1 active（验证取最大组）
    facts = [("panic", "active", 1), ("duration", "active", 1),
             ("dry_up", "inactive", 1),
             ("no_new_low_3w", "active", 9)]
    for sig, st, aid in facts:
        conn.execute(
            """INSERT INTO signal_facts (symbol, observed_on, signal, state,
               triggered, anchor_id, details_json, run_id, rule_version,
               config_hash, created_at)
               VALUES ('600000.SH','2024-02-16',?,? , 0, ?, '{}', 't','v1','h',
                       '2024-02-16')""", (sig, st, aid))
    conn.commit()
    conn.close()
    return path


class TestLoaders:
    def test_counts_takes_max_group(self, ev_db):
        from scripts.backtest.db import connect

        with connect(ev_db) as conn:
            counts = load_exhaustion_counts(conn, "600000.SH")
        assert counts == {"2024-02-16": (1, 2)}     # (anchor_id, n_active)

    def test_weeks_and_latest_before(self, ev_db):
        from scripts.backtest.db import connect

        with connect(ev_db) as conn:
            weeks = load_completed_weeks(conn, "600000.SH")
        assert weeks == ["2024-02-16", "2024-02-23"]
        assert latest_week_before(weeks, "2024-02-20") == "2024-02-16"
        assert latest_week_before(weeks, "2023-12-31") is None

    def test_anchors_filter_fallback_and_stop(self, ev_db):
        from scripts.backtest.db import connect

        with connect(ev_db) as conn:
            a = load_decline_starts(conn, "600000.SH", stop_pct=0.08)
        assert len(a) == 2                                  # fallback 被滤
        assert [x.anchor_id for x in a] == [1, 2]
        assert a[0].stop_adj == pytest.approx(round(11.0 * 1.0 * 0.92, 6))
        assert a[1].stop_adj == pytest.approx(round(12.0 * 1.0 * 0.92, 6))


def _mk_strategy(events, weeks, anchors, discount=0.0):
    cls = make_exhaustion_strategy(events, weeks, anchors,
                                   entry_discount_pct=discount)
    s = cls()
    s.week_counts = dict(cls.events_map_tuple)
    s.weeks = list(cls.weeks_tuple)
    s.anchors = list(cls.anchors_tuple)
    s._anchor_idx = -1
    s._entry_anchor = None
    return s


class TestStrategyUnit:
    def _bar(self, ts_day: str, close: float):
        import time as _t

        ns = int(pd.Timestamp(ts_day, tz="Asia/Shanghai").timestamp() * 1e9)
        return type("B", (), {"timestamp": ns, "close": close,
                              "symbol": "600000.SH"})()

    def test_episode_lock_and_anchor_advance_exit(self, ev_db):
        s = _mk_strategy({"2024-01-05": (1, 2)}, ["2024-01-05", "2024-01-12"],
                         [("2024-01-02", 10.12, 11.0)])
        orders = []
        s.order_target_percent = lambda **kw: orders.append(("buy", kw))
        s.close_position = lambda **kw: orders.append(("close", kw))
        pos = {"q": 0}
        s.get_position = lambda sym: pos["q"]

        b1 = self._bar("2024-01-05", 10.2)
        s.on_bar(b1); pos["q"] = 95                    # NextOpen 后视为已成交
        n_before = len([o for o in orders if o[0] == "buy"])
        s.on_bar(self._bar("2024-01-08", 10.3))        # 同 episode：不得再买
        assert len([o for o in orders if o[0] == "buy"]) == n_before

        # 新锚出现（idx 推进）→ episode 终结出场
        s.anchors.append(("2024-01-09", 9.0, 9.78))
        s.on_bar(self._bar("2024-01-10", 10.1))
        assert orders[-1][0] == "close"
        pos["q"] = 0

    def test_entry_discount_gate_binds(self, ev_db):
        s = _mk_strategy({"2024-01-05": (1, 2)}, ["2024-01-05"],
                         [("2024-01-02", 8.0, 10.0)], discount=0.15)
        orders = []
        s.order_target_percent = lambda **kw: orders.append(("buy", kw))
        s.get_position = lambda sym: 0
        s._advance_anchor("2099-01-01")
        s.on_bar(self._bar("2024-01-05", 9.5))         # 9.5 > 10×0.85=8.5 → 拒
        assert not orders
        s.on_bar(self._bar("2024-01-06", 8.4))         # ≤8.5 → 进
        assert orders and orders[-1][0] == "buy"


class TestEndToEnd:
    def test_missing_symbol_raises_cleanly(self):
        """无行情数据干净抛错（归上层 skipped 路径）。"""
        from scripts.backtest.run_event import run_event_one
        with pytest.raises(ValueError):
            run_event_one("999999.SZ", {}, stop_pct=0.08, min_signals=2)


def test_full_round_trip_akquant(ev_db):
    """端到端：入场事实满足→买入；收盘恒低于止损线→一笔清仓闭环。"""
    import logging
    logging.disable(logging.ERROR)
    from akquant import run_backtest

    from scripts.backtest.data import load_symbol
    from scripts.backtest.db import connect
    from scripts.backtest.event_signals import (
        load_completed_weeks,
        load_decline_starts,
        load_exhaustion_counts,
    )
    from scripts.backtest.run import build_kwargs, load_config

    with connect(ev_db) as conn:
        counts = {w: t for w, t in load_exhaustion_counts(conn, "600000.SH").items()}
        weeks = load_completed_weeks(conn, "600000.SH")
        anchors = load_decline_starts(conn, "600000.SH", stop_pct=0.08)
        df = load_symbol(conn, "600000.SH")
    # 入场周条件：n>=2（锚1 组 panic+duration）
    events = {w: t for w, t in counts.items() if t[1] >= 2}
    cls = make_exhaustion_strategy(events, weeks,
                                   [(a.trade_date, a.stop_adj) for a in anchors])
    res = run_backtest(data=df, strategy=cls(), symbols="600000.SH",
                       **build_kwargs(load_config()))
    trades = res.trades_df
    assert len(trades) >= 1                     # 完整开平至少一笔
    o = res.orders_df
    assert set(o["status"]) <= {"filled", "rejected"}
    # 入场后首日即触发止损（10 < 10.12）→ 卖出方向订单存在
    assert "sell" in set(o["side"])
