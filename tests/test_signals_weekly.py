"""D2.1 周线锚点 + 衰竭信号测试（设计 §9.2 硬门槛）。

覆盖：
- 每个阈值边界三态（等于/略高/略低）：放量 2 倍、下影/实体比、下影/振幅比、
  大阳线实体 60%、大阳线涨幅 5%、缩量 50%、3 周不创新低、8 周持续；
- 干涸型基数不足 4 周不判定；
- 底背离只在 pivot 确认日出现、不回填（构造序列锁定）；
- episode 结束（收盘高于下跌起点收盘）与恐慌活跃期到期（4 周）；
- 无未来函数：截断序列重算，前 N 周结果与全量算前 N 周一致。

纯函数边界测试用小窗口参数覆盖（窗口大小非阈值，阈值一律取 signals.yaml 默认）。
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import date, timedelta

import pytest

from scripts.pipeline import db
from scripts.signals import anchors, exhaustion
from scripts.signals.common import WeekBar, load_params
from scripts.signals.weekly_signals import recompute_weekly_signals

SYM = "TEST.SH"


def params_default() -> dict:
    p, _ = load_params()
    return p


def params_small() -> dict:
    """窗口缩小的测试参数（阈值保持默认）。"""
    p = copy.deepcopy(params_default())
    p["exhaustion"]["panic"]["vol_ma_weeks"] = 4
    return p


def wk(o, h, l, c, vol, week_end="2026-01-09", week_start="2026-01-05") -> WeekBar:
    return WeekBar(week_end, week_start, o, h, l, c, vol)


def _panic_series(vol_last: float, **last_bar) -> tuple[list[WeekBar], int]:
    """5 周序列：前 4 周量 100、收盘 20；末周给定量与 K 线。"""
    weeks = [wk(20, 21, 19, 20, 100, f"2026-01-{2 + 7 * k:02d}",
                f"2025-12-{29 + 7 * k:02d}") for k in range(4)]
    weeks.append(wk(last_bar.get("o", 20), last_bar.get("h", 21),
                    last_bar.get("l", 19), last_bar.get("c", 20), vol_last,
                    "2026-01-30", "2026-01-26"))
    return weeks, 4


# ---------------------------------------------------------------- 恐慌型阈值边界

def test_panic_volume_multiple_boundary():
    """放量阈值：量 = 均量×2 恰好触发；略低不触发；略高触发。"""
    p = params_small()
    for vol, expect in [(200.0, True), (199.99, False), (200.01, True)]:
        weeks, i = _panic_series(vol, o=10, c=10, h=12, l=8)  # 长下影形态满足
        assert exhaustion.panic_condition(weeks, i, p) is expect, vol


def test_panic_lower_shadow_body_ratio_boundary():
    """长下影：下影 = 实体 2 倍恰好满足；略低不满足。"""
    p = params_small()
    for low, expect in [(8.0, True), (8.001, False), (7.999, True)]:
        weeks, i = _panic_series(200.0, o=10, c=11, h=12, l=low)
        assert exhaustion.panic_condition(weeks, i, p) is expect, low


def test_panic_lower_shadow_range_pct_boundary():
    """长下影：下影 = 全周振幅 35% 恰好满足；略低不满足。"""
    p = params_small()
    for low, expect in [(8.0, True), (8.01, False), (7.99, True)]:
        weeks, i = _panic_series(200.0, o=9.4, c=9.9, h=12, l=low)
        assert exhaustion.panic_condition(weeks, i, p) is expect, low


def test_panic_big_yang_body_pct_boundary():
    """大阳线：实体 = 振幅 60% 恰好满足（涨幅达标）；略低不满足。"""
    p = params_small()
    for close, expect in [(16.0, True), (15.99, False), (16.01, True)]:
        weeks, i = _panic_series(200.0, o=10, h=20, l=10, c=close)
        weeks[i - 1] = wk(15, 16, 14, 15, 100, "2026-01-23", "2026-01-19")
        assert exhaustion.panic_condition(weeks, i, p) is expect, close


def test_panic_big_yang_gain_pct_boundary():
    """大阳线：周涨幅 = 5% 恰好满足（实体达标）；略低不满足。"""
    p = params_small()
    for close, expect in [(15.75, True), (15.74, False), (15.76, True)]:
        weeks, i = _panic_series(200.0, o=10, h=16, l=10, c=close)
        weeks[i - 1] = wk(15, 16, 14, 15, 100, "2026-01-23", "2026-01-19")
        assert exhaustion.panic_condition(weeks, i, p) is expect, close


# ---------------------------------------------------------------- 干涸型边界与基数不足

def _dryup_series(vol_last: float) -> list[WeekBar]:
    weeks = [wk(50, 51, 49, 50, 999, "2026-01-02", "2025-12-29")]
    friday = date(2026, 1, 9)
    for _ in range(6):  # idx1..6 量 100
        weeks.append(wk(40, 41, 39, 40, 100, friday.isoformat(),
                        (friday - timedelta(days=4)).isoformat()))
        friday += timedelta(days=7)
    weeks.append(wk(40, 41, 39, 40, vol_last, friday.isoformat(),
                    (friday - timedelta(days=4)).isoformat()))  # idx7
    return weeks


def test_dryup_vol_ratio_boundary():
    """缩量阈值：量 = 基数均量 50% 恰好触发；略高不触发；略低触发。"""
    p = params_default()
    for vol, expect in [(50.0, True), (50.01, False), (49.99, True)]:
        weeks = _dryup_series(vol)
        cond, det = exhaustion.dryup_state(weeks, 7, 2, p)  # decline=idx2, base=idx3..6
        assert cond is expect, (vol, det)
        assert det["base_mean"] == pytest.approx(100.0)


def test_dryup_insufficient_base_weeks_not_judged():
    """下跌起点后可用基数不足 4 周（不含当前周）→ 不判定（None）。"""
    p = params_default()
    weeks = _dryup_series(10.0)  # 量再低也不判定
    assert exhaustion.dryup_state(weeks, 6, 2, p)[0] is None  # base=idx3..5 仅 3 周
    assert exhaustion.dryup_state(weeks, 4, 2, p)[0] is None  # base=idx3 仅 1 周


# ---------------------------------------------------------------- 三周不创新低 / 持续时间

def _nnl_weeks(lows: list[float]) -> list[WeekBar]:
    friday = date(2026, 1, 2)
    out = []
    for l in lows:
        out.append(wk(20, 21, l, 20, 100, friday.isoformat(),
                      (friday - timedelta(days=4)).isoformat()))
        friday += timedelta(days=7)
    return out


def test_no_new_low_3w_states():
    """3 周不创新低：确认周触发并持有；确认前跌破永不触发；持有期跌破失效。"""
    p = params_default()
    weeks = _nnl_weeks([8.0, 8.5, 8.5, 8.5, 8.5, 7.9])  # anchor=idx0, low=8
    assert exhaustion.nnl_state(weeks, 2, 0, 8.0, p)[:2] == ("inactive", False)  # 等待
    st, trig, det = exhaustion.nnl_state(weeks, 3, 0, 8.0, p)
    assert (st, trig) == ("active", True) and det["reason"] == "confirmed"
    st, trig, det = exhaustion.nnl_state(weeks, 4, 0, 8.0, p)
    assert (st, trig) == ("active", False) and det["reason"] == "holding"
    st, _, det = exhaustion.nnl_state(weeks, 5, 0, 8.0, p)  # idx5 跌破锚点最低
    assert st == "inactive" and det["reason"] == "new_low"

    weeks_broken = _nnl_weeks([8.0, 8.5, 7.9, 8.5, 8.5])
    st, _, det = exhaustion.nnl_state(weeks_broken, 3, 0, 8.0, p)
    assert st == "inactive" and det["reason"] == "broken_before_confirm"


def test_duration_weeks_boundary():
    """持续时间：距下跌起点恰好 8 个完成周触发；7 周不触发；9 周持有。"""
    p = params_default()
    assert exhaustion.duration_state(10, 2, p)[0] is True   # elapsed=8
    assert exhaustion.duration_state(9, 2, p)[0] is False   # elapsed=7
    assert exhaustion.duration_state(11, 2, p)[0] is True   # elapsed=9


# ---------------------------------------------------------------- 锚点识别（纯函数）

def _daily_for_weeks(weeks: list[WeekBar]) -> dict:
    """周内最低日 = 周三，周末日 close_raw = 周收盘（因子 1.0）。"""
    daily = {}
    for w in weeks:
        we = date.fromisoformat(w.week_end_date)
        for k in range(5):
            d = (we - timedelta(days=4 - k)).isoformat()
            daily[d] = {
                "low_adj": w.low if k == 2 else w.low + 1.0,
                "low_raw": w.low if k == 2 else w.low + 1.0,
                "close_raw": w.close if k == 4 else w.close + 0.5,
            }
    return daily


def test_anchor_fallback_and_decline_start():
    """无恐慌 → fallback 锚点（26 周窗最低收盘周，周内最低日）；下跌起点取最高收盘周。"""
    p = params_default()
    closes = [50, 49, 48, 47, 46, 45]
    friday = date(2026, 1, 2)
    weeks = []
    for c in closes:
        weeks.append(wk(c, c + 2, c - 2, c, 100, friday.isoformat(),
                        (friday - timedelta(days=4)).isoformat()))
        friday += timedelta(days=7)
    steps = anchors.compute_anchor_timeline(weeks, _daily_for_weeks(weeks), p)
    s = steps[5]
    assert s.panic.is_fallback and s.panic.week_index == 5
    assert s.panic.trade_date == (date(2026, 2, 6) - timedelta(days=2)).isoformat()  # 周三
    assert s.panic.adjusted_price == pytest.approx(43.0)  # 45 - 2
    assert s.decline.week_index == 0 and s.decline.adjusted_price == pytest.approx(50.0)
    assert s.decline.trade_date == "2026-01-02"  # 周末日
    assert s.decline.raw_price == pytest.approx(50.0)
    assert steps[0].decline is None  # 首周之前无完成周


def test_anchor_tie_rules():
    """平值规则：fallback 最低收盘平值取最近周；下跌起点最高收盘平值取离恐慌低点最近周。"""
    p = params_default()
    closes = [50, 50, 48, 45, 45]  # decline 平值 idx0/1 → 取 idx1；fallback 平值 idx3/4 → 取 idx4
    friday = date(2026, 1, 2)
    weeks = []
    for c in closes:
        weeks.append(wk(c, c + 2, c - 2, c, 100, friday.isoformat(),
                        (friday - timedelta(days=4)).isoformat()))
        friday += timedelta(days=7)
    steps = anchors.compute_anchor_timeline(weeks, _daily_for_weeks(weeks), p)
    s = steps[4]
    assert s.panic.week_index == 4
    assert s.decline.week_index == 1  # 平值取离恐慌低点最近（索引最大）


# ---------------------------------------------------------------- 集成夹具

def build_db(tmp_path, specs, symbol=SYM) -> sqlite3.Connection:
    """specs: list[dict(o,h,l,c,vol,rsi=,hist=)]，逐周插入 weekly/daily/indicators。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = db.connect(tmp_path / "market.db")
    db.migrate(conn)
    now = db.utc_now()
    friday = date(2026, 1, 2)
    for spec in specs:
        we = friday.isoformat()
        ws = (friday - timedelta(days=4)).isoformat()
        conn.execute(
            """
            INSERT INTO weekly_bars (symbol, week_end_date, week_start_date,
                open_adj, high_adj, low_adj, close_adj, volume_adj, amount_raw,
                trading_days, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 5, 'test')
            """,
            (symbol, we, ws, spec["o"], spec["h"], spec["l"], spec["c"], spec["vol"]),
        )
        for k in range(5):
            d = (friday - timedelta(days=4 - k)).isoformat()
            conn.execute(
                """
                INSERT INTO daily_bars (symbol, trade_date, market,
                    open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                    currency, price_adj_factor, share_factor, trading_status,
                    source, raw_object_id, updated_at)
                VALUES (?, ?, 'CN', NULL, NULL, ?, ?, NULL, NULL, 'CNY', 1.0, 1.0,
                        'normal', 'test', NULL, ?)
                """,
                (symbol, d,
                 spec["l"] if k == 2 else spec["l"] + 1.0,
                 spec["c"] if k == 4 else spec["c"] + 0.5,
                 now),
            )
        conn.execute(
            """
            INSERT INTO indicators_weekly (symbol, week_end_date, rsi12, macd_hist,
                                           computed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (symbol, we, spec.get("rsi"), spec.get("hist"), now),
        )
        friday += timedelta(days=7)
    conn.commit()
    return conn


def week_ends(conn, symbol=SYM) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT week_end_date FROM weekly_bars WHERE symbol = ? ORDER BY week_end_date",
        (symbol,))]


def facts(conn, symbol=SYM) -> dict:
    return {(r["signal"], r["observed_on"]): r for r in conn.execute(
        "SELECT * FROM signal_facts WHERE symbol = ?", (symbol,))}


def _divergence_specs(n=30) -> list[dict]:
    """两个 pivot low：idx10（l=5, c=20, rsi=40）、idx20（l=6, c=19, rsi=50）。
    其余周 low=10 平值（严格小于规则下不构成 pivot）。"""
    specs = []
    for k in range(n):
        s = {"o": 20.0, "h": 21.0, "l": 10.0, "c": 20.0, "vol": 100.0,
             "rsi": 40.0, "hist": 0.1}
        if k == 10:
            s.update(l=5.0)
        if k == 20:
            s.update(l=6.0, c=19.0, rsi=50.0)
        specs.append(s)
    return specs


def test_divergence_only_at_confirmation_no_backfill(tmp_path):
    """底背离：只在 pivot 确认周（pivot+2）触发并活跃 4 周，不回填到 pivot 周。"""
    conn = build_db(tmp_path, _divergence_specs())
    with conn:
        recompute_weekly_signals(conn, SYM, params=params_default(), config_hash="t")
    f = facts(conn)
    we = week_ends(conn)
    p1, p2, confirm = we[10], we[20], we[22]
    assert f[("divergence", confirm)]["triggered"] == 1
    assert f[("divergence", confirm)]["state"] == "active"
    # 不回填：pivot 发生周无触发、不活跃
    assert f[("divergence", p2)]["triggered"] == 0
    assert f[("divergence", p2)]["state"] == "inactive"
    assert f[("divergence", p1)]["state"] == "inactive"
    # 首个 pivot 确认周只有一个 pivot → 不触发
    assert f[("divergence", we[12])]["triggered"] == 0
    # 活跃 4 周（确认周起）：22,23,24,25 活跃，26 到期
    for k in range(22, 26):
        assert f[("divergence", we[k])]["state"] == "active", k
    assert f[("divergence", we[26])]["state"] == "inactive"
    assert f[("divergence", confirm)]["active_until"] == we[25]
    conn.close()


def test_episode_end_and_duration(tmp_path):
    """episode 结束：收盘高于下跌起点收盘 → 该锚点全部信号 inactive（含 duration）。"""
    closes = [50, 50, 50, 50, 46, 45, 44, 43, 42, 41, 40, 49, 55, 55, 55, 55]
    specs = [{"o": c, "h": c + 2, "l": c - 2, "c": c, "vol": 100.0,
              "rsi": 40.0, "hist": 0.1} for c in closes]
    conn = build_db(tmp_path, specs)
    with conn:
        recompute_weekly_signals(conn, SYM, params=params_default(), config_hash="t")
    f = facts(conn)
    we = week_ends(conn)
    # idx11：fallback 锚点 idx10（收盘 40 最低），下跌起点 idx3（收盘 50，平值取近）
    # elapsed = 11-3 = 8 → duration 触发且活跃（收盘 49 未破 50）
    d11 = f[("duration", we[11])]
    assert d11["state"] == "active" and d11["triggered"] == 1
    # idx12：收盘 55 > 下跌起点收盘 50 → episode 结束，duration 转 inactive
    d12 = f[("duration", we[12])]
    assert d12["state"] == "inactive"
    import json as _json
    assert _json.loads(d12["details_json"])["reason"] == "episode_ended"
    conn.close()


def _panic_fixture_specs(n=12) -> list[dict]:
    """idx4 恐慌（量 200 = 4 周均量 2 倍 + 长下影）；idx9 跌破锚点最低价。"""
    specs = []
    for k in range(n):
        s = {"o": 45.0, "h": 47.0, "l": 44.5, "c": 45.0, "vol": 100.0,
             "rsi": 40.0, "hist": 0.1}
        if k < 4:
            s.update(h=47.0, l=44.0)
        if k == 4:
            s.update(vol=200.0, l=43.0)
        if k == 9:
            s.update(l=42.0)
        specs.append(s)
    return specs


def test_panic_active_expiry_nnl_and_count(tmp_path):
    """恐慌活跃 4 周到期；三周不创新低确认/跌破失效；活跃计数供档位触发。"""
    conn = build_db(tmp_path, _panic_fixture_specs())
    with conn:
        recompute_weekly_signals(conn, SYM, params=params_small(), config_hash="t")
    f = facts(conn)
    we = week_ends(conn)
    # panic：idx4 触发，活跃 idx4..7，idx8 到期
    assert f[("panic", we[4])]["triggered"] == 1
    for k in range(4, 8):
        assert f[("panic", we[k])]["state"] == "active", k
    assert f[("panic", we[4])]["active_until"] == we[7]
    assert f[("panic", we[8])]["state"] == "inactive"
    # nnl：锚点 idx4（最低 43），idx5-7 未破 → idx7 确认触发；idx9 跌破失效
    assert f[("no_new_low_3w", we[7])]["triggered"] == 1
    assert f[("no_new_low_3w", we[8])]["state"] == "active"
    assert f[("no_new_low_3w", we[9])]["state"] == "inactive"
    # 活跃计数：idx7 → panic + nnl = 2 项达标；idx8 → 仅 nnl = 1 项
    c7 = exhaustion.count_active_signals(conn, SYM, we[7], min_active=2)
    assert c7["active_count"] == 2 and c7["meets_min"] is True
    assert set(c7["active_signals"]) == {"panic", "no_new_low_3w"}
    c8 = exhaustion.count_active_signals(conn, SYM, we[8], min_active=2)
    assert c8["active_count"] == 1 and c8["meets_min"] is False
    conn.close()


# ---------------------------------------------------------------- 无未来函数 / 幂等

def test_no_future_function_truncation(tmp_path):
    """截断序列重算：前 N 周 signal_facts 与全量算前 N 周完全一致（§5.1）。"""
    specs = _divergence_specs(30)
    p = params_default()
    conn_full = build_db(tmp_path / "full", specs)
    with conn_full:
        recompute_weekly_signals(conn_full, SYM, params=p, config_hash="t")
    n = 18
    conn_part = build_db(tmp_path / "part", specs[:n])
    with conn_part:
        recompute_weekly_signals(conn_part, SYM, params=p, config_hash="t")
    cut = week_ends(conn_part)[-1]

    def snap(conn):
        return sorted(
            (r["signal"], r["observed_on"], r["state"], r["triggered"],
             r["active_until"], r["details_json"])
            for r in conn.execute(
                "SELECT * FROM signal_facts WHERE symbol = ? AND observed_on <= ?",
                (SYM, cut)))

    assert snap(conn_part) == snap(conn_full)
    conn_full.close()
    conn_part.close()


def test_rebuild_idempotent(tmp_path):
    """派生表 DELETE+重插：重跑结果一致，无重复行。"""
    conn = build_db(tmp_path, _divergence_specs())
    with conn:
        recompute_weekly_signals(conn, SYM, run_id="run_a",
                                 params=params_default(), config_hash="t")
    first = conn.execute(
        "SELECT COUNT(*) FROM signal_facts WHERE symbol = ?", (SYM,)).fetchone()[0]
    with conn:
        recompute_weekly_signals(conn, SYM, run_id="run_b",
                                 params=params_default(), config_hash="t")
    second = conn.execute(
        "SELECT COUNT(*) FROM signal_facts WHERE symbol = ?", (SYM,)).fetchone()[0]
    assert first == second == 30 * 5
    anchors_n = conn.execute(
        "SELECT COUNT(*) FROM weekly_anchors WHERE symbol = ?", (SYM,)).fetchone()[0]
    assert anchors_n > 0
    conn.close()
