"""吸筹形态状态机测试（设计 §5.5，方法来源《如何看出主力吸筹》三阶段框架）。

覆盖：
- 放量破位边界：恰好 -5% / 恰好 2 倍量 / 非 60 日新低 / 样本不足；
- 缩量横盘三条件（振幅 / 缩量 / MA 粘合）与箱体取窗口收盘价；
- 试盘判定边界（振幅 / 上影占比 / 放量）；
- 状态机全路径：idle → watching → consolidating → confirmed（terminal 次日回 idle）；
- 失效路径：跌破箱体下沿 box_broken；MA 缺失不确认，超期 expired_no_consolidation；
- 数据缺失纪律：OHLCV 缺失行剔除并记 degraded；前收 ≤0 不除零、记 invalid_prev_close；
- expired_consolidation 从进入 consolidating 起算（§5.4c），不从破位日起算；
- 全量重算幂等（DELETE+重插，行数一致）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from scripts.pipeline import db
from scripts.signals import accumulation as acc
from scripts.signals.common import load_params

SYM = "TEST.SH"
P = load_params()[0]


# ---------------------------------------------------------------- 夹具

def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    return conn


def day(i: int) -> str:
    return (date(2026, 1, 5) + timedelta(days=i)).isoformat()


def add_bar(conn, i, *, open_=100.0, high=101.0, low=99.0, close=100.0,
            volume=100.0, symbol=SYM):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
            source, updated_at)
        VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, 1.0, 1.0, 'test', ?)
        """,
        (symbol, day(i), open_, high, low, close, volume, db.utc_now()),
    )


def add_ma(conn, i, ma5=94.1, ma10=94.05, ma20=94.0, symbol=SYM):
    conn.execute(
        """
        INSERT INTO indicators_daily (symbol, trade_date, ma5, ma10, ma20,
            computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (symbol, day(i), ma5, ma10, ma20, db.utc_now()),
    )


def facts(conn, symbol=SYM):
    rows = conn.execute(
        "SELECT observed_on, state, triggered, details_json FROM signal_facts "
        "WHERE symbol = ? AND signal = 'accumulation' ORDER BY observed_on",
        (symbol,),
    ).fetchall()
    return [(r["observed_on"], r["state"], r["triggered"],
             json.loads(r["details_json"])) for r in rows]


def flat_days(conn, start, n, *, close=100.0, volume=100.0):
    for i in range(start, start + n):
        add_bar(conn, i, open_=close, high=close + 1.0, low=close - 1.0,
                close=close, volume=volume)


def build_consolidating(conn):
    """70 天平盘 + 放量破位 + 10 天缩量横盘 → consolidating，返回破位/横盘确认索引。"""
    flat_days(conn, 0, 70)                                # 索引 0..69：均量基数 100
    add_bar(conn, 70, open_=100.0, high=100.5, low=93.5,  # 破位日：-6%、3 倍量、60 日新低
            close=94.0, volume=300.0)
    closes = [94.0, 94.2, 93.9, 94.1, 94.0, 94.2, 93.9, 94.1, 94.0, 94.2]
    for k, c in enumerate(closes):                        # 索引 71..80：缩量横盘窗口
        add_bar(conn, 71 + k, open_=c, high=c + 0.3, low=c - 0.3,
                close=c, volume=50.0)
        add_ma(conn, 71 + k)
    return 70, 80   # 破位索引、横盘确认索引（days_since = 10 = min_days）


# ---------------------------------------------------------------- 单项判定边界

def test_breakdown_boundaries():
    """恰好 -5% 且恰好 2 倍量且创 60 日新低 → 破位成立；任一不满足不成立。"""
    bars = [{"trade_date": day(i), "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.0, "volume": 100.0} for i in range(70)]
    i = 69
    base = acc.vol_base(bars, i, P["accumulation"]["vol_ma_days"])
    assert base == 100.0
    for chg_pct, vol, new_low_close, expect in [
            (-0.05, 200.0, 95.0, True),    # 恰好 -5% + 恰好 2 倍量 + 新低
            (-0.0499, 200.0, 95.0, False), # 跌幅略不足
            (-0.06, 199.9, 95.0, False),   # 量略不足
            (-0.06, 200.0, 100.0, False)]: # 非新低
        bars[i] = {"trade_date": day(i), "open": 100.0, "high": 101.0,
                   "low": new_low_close, "close": new_low_close, "volume": vol}
        prev = bars[i - 1]["close"]
        # 调整 close 使精确命中目标跌幅（相对前收 100）
        bars[i]["close"] = prev * (1 + chg_pct) if new_low_close < 100 else new_low_close
        bars[i]["low"] = min(bars[i]["low"], bars[i]["close"])
        cond, det = acc.is_breakdown(bars, i, base, P["accumulation"])
        assert cond is expect, (chg_pct, vol, det)


def test_breakdown_insufficient_history():
    bars = [{"trade_date": day(i), "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.0, "volume": 100.0} for i in range(10)]
    cond, det = acc.is_breakdown(bars, 5, 100.0, P["accumulation"])
    assert cond is False and det["reason"] == "insufficient_history"
    assert acc.vol_base(bars, 5, 20) is None  # 样本不足不冒充（§4.1）


def test_probe_boundaries():
    p = P["accumulation"]
    # 振幅恰好 3%、上影恰好 50%、量恰好 1.5 倍 → 成立
    bar = {"trade_date": "2026-01-05", "open": 94.0, "high": 97.0, "low": 93.97,
           "close": 94.06, "volume": 150.0}
    prev_close = 100.0
    rng = bar["high"] - bar["low"]
    bar["close"] = bar["high"] - 0.5 * rng   # 上影 = 50% 振幅，close > open 不限
    cond, det = acc.is_probe(bar, prev_close, 100.0, p)
    assert det["amplitude"] == rng / prev_close
    assert abs(det["upper_shadow_range_pct"] - 0.5) < 1e-9
    assert cond is True
    # 量不足 → 不成立
    cond, _ = acc.is_probe({**bar, "volume": 149.9}, prev_close, 100.0, p)
    assert cond is False
    # 上影不足 → 不成立
    bar2 = {**bar, "close": bar["high"] - 0.49 * rng}
    cond, _ = acc.is_probe(bar2, prev_close, 100.0, p)
    assert cond is False


# ---------------------------------------------------------------- 状态机全路径

def test_full_path_to_confirmed(tmp_path):
    conn = make_conn(tmp_path)
    with conn:
        bd_idx, consol_idx = build_consolidating(conn)
        # 试盘日：振幅 ~3.4%、上影 87.5%、2 倍量（对横盘期均量基数 50）
        add_bar(conn, 81, open_=94.0, high=97.0, low=93.8, close=94.1, volume=200.0)
        add_ma(conn, 81)
        # 突破日：收阳、收盘 > 箱体上沿 94.2、放量
        add_bar(conn, 82, open_=94.3, high=95.4, low=94.2, close=95.0, volume=200.0)
        add_ma(conn, 82)
        # terminal 次日回 idle
        add_bar(conn, 83, open_=95.0, high=96.0, low=94.5, close=95.0, volume=120.0)
        add_ma(conn, 83)
        res = acc.run_accumulation(conn, SYM)

    rows = facts(conn)
    by_day = {r[0]: r for r in rows}
    assert by_day[day(bd_idx)][1] == "watching" and by_day[day(bd_idx)][2] == 1
    assert by_day[day(bd_idx)][3]["reason"] == "breakdown_detected"
    c = by_day[day(consol_idx)]
    assert c[1] == "consolidating" and c[2] == 1
    assert c[3]["reason"] == "consolidation_confirmed"
    assert c[3]["box_low"] == 93.9 and c[3]["box_high"] == 94.2  # 收盘价定界
    probe = by_day[day(81)]
    assert probe[1] == "consolidating" and probe[2] == 0        # 试盘不产生状态转换
    assert probe[3]["probe_today"] is True and probe[3]["probe_count"] == 1
    conf = by_day[day(82)]
    assert conf[1] == "confirmed" and conf[2] == 1
    assert conf[3]["reason"] == "breakout_confirmed"
    assert by_day[day(83)][1] == "idle"                         # terminal 次日回 idle
    assert res.latest["state"] == "idle"
    assert any(t["state"] == "confirmed" for t in res.transitions)


def test_failure_box_broken(tmp_path):
    conn = make_conn(tmp_path)
    with conn:
        _, consol_idx = build_consolidating(conn)
        add_bar(conn, 81, open_=94.0, high=94.1, low=92.8, close=93.0, volume=60.0)
        add_ma(conn, 81)
        acc.run_accumulation(conn, SYM)
    rows = facts(conn)
    by_day = {r[0]: r for r in rows}
    f = by_day[day(81)]
    assert f[1] == "failed" and f[2] == 1 and f[3]["reason"] == "box_broken"


def test_missing_ma_never_confirms_then_expires(tmp_path):
    """MA 缺失 → 横盘不可判定（不猜，§2.5）；破位后超 120 日 → expired。"""
    conn = make_conn(tmp_path)
    with conn:
        flat_days(conn, 0, 70)
        add_bar(conn, 70, open_=100.0, high=100.5, low=93.5, close=94.0,
                volume=300.0)
        flat_days(conn, 71, 130, close=94.0, volume=50.0)  # 无 indicators_daily 行
        acc.run_accumulation(conn, SYM)
    rows = facts(conn)
    by_day = {r[0]: r for r in rows}
    assert by_day[day(80)][3]["reason"] == "missing_ma"   # min_days 当日即因缺 MA 不判定
    expire_day = day(70 + 121)                            # days_since > 120
    f = by_day[expire_day]
    assert f[1] == "failed" and f[2] == 1
    assert f[3]["reason"] == "expired_no_consolidation"


def test_recompute_idempotent(tmp_path):
    conn = make_conn(tmp_path)
    with conn:
        build_consolidating(conn)
        r1 = acc.run_accumulation(conn, SYM)
        r2 = acc.run_accumulation(conn, SYM)
    assert r1.facts_written == r2.facts_written
    assert len(facts(conn)) == r1.facts_written
    run = conn.execute(
        "SELECT stage, status FROM pipeline_runs WHERE run_id = ?",
        (r2.run_id,)).fetchone()
    assert run["stage"] == "accumulation" and run["status"] == "success"


# ---------------------------------------------------------------- 数据缺失纪律

def add_bar_null_close(conn, i, symbol=SYM):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
            source, updated_at)
        VALUES (?, ?, 'CN', 100.0, 101.0, 99.0, NULL, 100.0, 1.0, 1.0, 'test', ?)
        """,
        (symbol, day(i), db.utc_now()),
    )


def test_missing_ohlcv_rows_skipped(tmp_path):
    """OHLCV 缺失行拒绝参与计算（§2.5 不猜，不当 0）：不崩溃、该行无 facts、记 degraded。"""
    conn = make_conn(tmp_path)
    with conn:
        flat_days(conn, 0, 5)
        add_bar_null_close(conn, 5)
        add_bar_null_close(conn, 6)
        flat_days(conn, 7, 23)
        res = acc.run_accumulation(conn, SYM)
    rows = facts(conn)
    days_in_facts = {r[0] for r in rows}
    assert day(5) not in days_in_facts and day(6) not in days_in_facts
    assert len(rows) == 28
    assert res.status == "degraded" and res.reason == "missing_ohlcv_rows"


def test_zero_prev_close_no_division(tmp_path):
    """前收 =0 → 涨跌幅不判定（invalid_prev_close），不除零，记 degraded。"""
    # 纯函数边界：prev close = 0 直接返回，不抛 ZeroDivisionError
    bars = [{"trade_date": day(i), "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.0, "volume": 100.0} for i in range(70)]
    bars[69]["close"] = 0.0
    bars.append({"trade_date": day(70), "open": 100.0, "high": 100.5,
                 "low": 93.5, "close": 94.0, "volume": 300.0})
    cond, det = acc.is_breakdown(bars, 70, 100.0, P["accumulation"])
    assert cond is False and det["reason"] == "invalid_prev_close"

    # 端到端：close_raw=0.0 的脏数据行（非 NULL，不剔除），次日不除零
    conn = make_conn(tmp_path)
    with conn:
        flat_days(conn, 0, 70)
        add_bar(conn, 70, open_=100.0, high=100.5, low=0.0, close=0.0, volume=50.0)
        add_bar(conn, 71, open_=100.0, high=100.5, low=93.5, close=94.0,
                volume=300.0)
        res = acc.run_accumulation(conn, SYM)
    by_day = {r[0]: r for r in facts(conn)}
    assert by_day[day(71)][3]["reason"] == "invalid_prev_close"
    assert res.status == "degraded" and res.reason == "invalid_prev_close"


def test_expired_consolidation_counts_from_consolidation_start(tmp_path):
    """expired_consolidation 从进入 consolidating 起算（§5.4c），不从破位日起算。"""
    conn = make_conn(tmp_path)
    with conn:
        _, consol_idx = build_consolidating(conn)      # 破位索引 70，横盘确认索引 80
        flat_days(conn, 81, 121, close=94.0, volume=50.0)  # 索引 81..201 箱体内平盘
        acc.run_accumulation(conn, SYM)
    by_day = {r[0]: r for r in facts(conn)}
    # 旧起算点（破位 +121 日 = 索引 191）不得 expired，证明不从破位日起算
    assert by_day[day(191)][1] == "consolidating"
    # 横盘第 120 天（索引 consol_idx+120）仍持有；第 121 天 expired
    assert by_day[day(consol_idx + 120)][1] == "consolidating"
    assert by_day[day(consol_idx + 120)][3]["days_consolidating"] == 120
    f = by_day[day(consol_idx + 121)]
    assert f[1] == "failed" and f[2] == 1
    assert f[3]["reason"] == "expired_consolidation"
