"""D2.2 日频监测 + D2.3 右侧确认状态机测试（设计 §9.2 硬门槛）。

覆盖：
- 证伪线：恰好 1% 跌破（≤ 锁定）、连续 2 日确认 / 只 1 日不确认 / 未跌破三态；
- 档位临近 3% 边界（恰好 / 略超 / 区内不计临近）；
- 档位触发：第一档无信号要求；第二档进区但同锚点活跃衰竭信号 <2 不触发，
  补 2 项后触发；
- 口径纪律：除权数据（因子 2.0）下现价 vs 复权 MA20 用折回值比较，
  直接跨尺度比会得出相反结论；
- 状态机三条路径 confirmed / invalidated / expired 全覆盖 + 突破量价边界；
- 无 active 卡片 → incomplete（no_active_card），不产出卡片信号（§2.5）；
- 公司行为冻结期间卡片触发挂起（§5.4b）。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from scripts.pipeline import db
from scripts.signals import corporate_action as ca_mod
from scripts.signals import daily_watch as dw
from scripts.signals import right_side as rs
from scripts.signals.common import load_params

SYM = "TEST.SH"


# ---------------------------------------------------------------- 夹具

def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    return conn


def add_bar(conn, day, close, *, low=None, volume=100.0, factor=1.0,
            share_factor=1.0, symbol=SYM):
    low = close if low is None else low
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
            source, updated_at)
        VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, ?, ?, 'test', ?)
        """,
        (symbol, day, close, max(close, low), low, close, volume, factor,
         share_factor, db.utc_now()),
    )


def add_null_bar(conn, day, *, close=None, low=None, volume=None, symbol=SYM):
    """允许 OHLCV 关键字段为 NULL 的 bar（缺失数据用例，schema 可空）。"""
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
            source, updated_at)
        VALUES (?, ?, 'CN', NULL, NULL, ?, ?, ?, 1.0, 1.0, 'test', ?)
        """,
        (symbol, day, low, close, volume, db.utc_now()),
    )


def add_card(conn, symbol=SYM, eff_from="2026-01-05", *, card_id="cv1",
             tiers=((1, "90.00", "95.00"), (2, "50.00", "60.00")),
             line="45.00", box=None, trigger=None, status="active"):
    conn.execute(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status,
            schema_version, created_at, effective_from, effective_to, supersedes_id,
            currency, price_basis, earnings_scenarios_json,
            valuation_scenarios_json, price_tiers_json, invalidation_json,
            swing_box_json, right_side_trigger_json, next_review_at,
            input_snapshot_json, run_id)
        VALUES (?, ?, ?, 'card_v1', ?, ?, NULL, NULL, 'CNY', 'raw',
                NULL, NULL, ?, ?, ?, ?, NULL, NULL, 'test')
        """,
        (
            card_id, symbol, status, db.utc_now(), eff_from,
            json.dumps({"tiers": [{"tier": t, "zone_low": lo, "zone_high": hi}
                                  for t, lo, hi in (tiers or [])]}, sort_keys=True),
            json.dumps({"line": line}) if line else None,
            json.dumps(box, sort_keys=True) if box else None,
            json.dumps({"trigger_level": trigger[0], "stop_level": trigger[1]},
                       sort_keys=True) if trigger else None,
        ),
    )


def add_weekly_signals(conn, week_end, states, symbol=SYM, anchor_id=1):
    """states: {signal: state}，写周线衰竭信号行（供档位触发计数）。"""
    for sig, state in states.items():
        conn.execute(
            """
            INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
                triggered, active_until, details_json, run_id, rule_version,
                config_hash, created_at)
            VALUES (?, ?, ?, ?, ?, 0, NULL, '{}', 'test', 'signals_v1', 'h', ?)
            """,
            (symbol, week_end, sig, state, anchor_id, db.utc_now()),
        )


def facts(conn, signal, symbol=SYM):
    rows = conn.execute(
        "SELECT observed_on, state, triggered, details_json FROM signal_facts "
        "WHERE symbol = ? AND signal = ? ORDER BY observed_on",
        (symbol, signal),
    ).fetchall()
    return [(r["observed_on"], r["state"], r["triggered"],
             json.loads(r["details_json"])) for r in rows]


P = load_params()[0]


# ---------------------------------------------------------------- 证伪线（§5.4）

def test_falsification_exactly_one_pct_boundary():
    """恰好低于证伪线 1% 算跌破（≤ 锁定）；略高不算；更低算。"""
    line = Decimal("100.00")
    for close, expect in [("99.00", True), ("99.01", False), ("98.99", True)]:
        det, run = dw.falsification_update(Decimal(close), line, 0.01, 2, 0)
        assert det["breached_today"] is expect, (close, det)
        assert run == (1 if expect else 0)


def test_falsification_confirm_days():
    """连续 2 日跌破 → 第 2 日确认（triggered）；只 1 日 → watching 不确认。"""
    line = Decimal("100.00")
    det1, run = dw.falsification_update(Decimal("99.00"), line, 0.01, 2, 0)
    assert (det1["state"], det1["triggered"], run) == ("watching", 0, 1)
    det2, run = dw.falsification_update(Decimal("99.00"), line, 0.01, 2, run)
    assert (det2["state"], det2["triggered"], run) == ("active", 1, 2)
    det3, run = dw.falsification_update(Decimal("98.50"), line, 0.01, 2, run)
    assert (det3["state"], det3["triggered"], run) == ("active", 0, 3)  # holding
    # 收回线上 → recovered；再次跌破只 1 日 → 重新计 watching
    det4, run = dw.falsification_update(Decimal("100.50"), line, 0.01, 2, run)
    assert (det4["state"], det4["reason"], run) == ("inactive", "recovered", 0)
    det5, run = dw.falsification_update(Decimal("99.00"), line, 0.01, 2, run)
    assert (det5["state"], det5["triggered"], run) == ("watching", 0, 1)


def test_falsification_integration(tmp_path):
    """入库序列：跌破1日→收回→连续2日跌破；signal_facts 逐日状态正确。"""
    conn = make_conn(tmp_path)
    add_card(conn, line="100.00")
    closes = [("2026-01-05", 99.00), ("2026-01-06", 100.50),
              ("2026-01-07", 99.00), ("2026-01-08", 99.00)]
    for d, c in closes:
        add_bar(conn, d, c)
    with conn:
        res = dw.run_daily_watch(conn, SYM)
    rows = facts(conn, "falsification_breach")
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("2026-01-05", "watching", 0), ("2026-01-06", "inactive", 0),
        ("2026-01-07", "watching", 0), ("2026-01-08", "active", 1),
    ]
    assert rows[-1][3]["consecutive_breach_days"] == 2
    assert rows[-1][3]["card_version_id"] == "cv1"
    conn.close()


# ---------------------------------------------------------------- 档位临近 / 触发（§5.4）

def _tiers():
    return [{"tier": 1, "zone_low": Decimal("100.00"), "zone_high": Decimal("110.00")},
            {"tier": 2, "zone_low": Decimal("50.00"), "zone_high": Decimal("60.00")}]


def test_tier_proximity_boundary():
    """距边界恰好 3% 临近（≤ 锁定）；略超不临近；价区内不计临近。"""
    for close, expect in [("97.00", True), ("96.99", False), ("97.01", True),
                          ("105.00", False)]:
        prox, _ = dw.tier_states(Decimal(close), _tiers(), 0.03, False, 2)
        assert (prox["state"] == "active") is expect, (close, prox)


def test_tier_trigger_signal_requirement():
    """第一档进区直接触发；第二档进区但活跃信号 <2 → pending_signals 不触发。"""
    # 第一档：无需信号
    _, trig = dw.tier_states(Decimal("105.00"), _tiers(), 0.03, False, 2)
    assert (trig["state"], trig["triggered"]) == ("triggered", 1)
    # 第二档：信号不足 → 不触发；补足 → 触发
    _, trig = dw.tier_states(Decimal("55.00"), _tiers(), 0.03, False, 2)
    assert (trig["state"], trig["triggered"]) == ("pending_signals", 0)
    _, trig = dw.tier_states(Decimal("55.00"), _tiers(), 0.03, True, 2)
    assert (trig["state"], trig["triggered"]) == ("triggered", 1)


def test_tier_trigger_integration_with_weekly_signals(tmp_path):
    """第二档触发依赖同 anchor 活跃衰竭信号 ≥2（count_active_signals 口径）。"""
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-01-09", 55.00)  # 进第二档 [50,60]
    with conn:
        dw.run_daily_watch(conn, SYM)
    row = facts(conn, "tier_triggered")[-1]
    assert (row[1], row[2]) == ("pending_signals", 0)
    assert row[3]["active_count"] == 0

    # 补完成周 2026-01-09 的 2 项活跃信号（同 anchor_id=1）后重算 → 触发
    add_weekly_signals(conn, "2026-01-09", {"duration": "active", "panic": "active"})
    with conn:
        dw.run_daily_watch(conn, SYM)
    row = facts(conn, "tier_triggered")[-1]
    assert (row[1], row[2]) == ("triggered", 1)
    assert row[3]["active_count"] == 2 and row[3]["anchor_id"] == 1
    conn.close()


# ---------------------------------------------------------------- 口径纪律（§5.4、§9.5）

def test_ma_comparison_adjustment_discipline():
    """复权 MA20 ÷ 当日因子折回后再比：因子 2.0、MA20(复权)=200 → 折回 100，
    现价 110 在均线上方；若跨尺度直接比（110 vs 200）会误判为下方。"""
    det = dw.ma_comparison_state(110.0, 2.0, {"ma20": 200.0, "ma60": None})
    assert det["state"] == "above"
    assert det["mas"]["ma20"]["raw_equiv"] == pytest.approx(100.0)
    # 跨尺度直接比较会得出 below —— 显式锁定差异，防回归
    assert ("below" if 110.0 < 200.0 else "above") != det["state"]


def test_ma_comparison_integration_ex_dividend(tmp_path):
    """构造除权数据（因子 2.0）走完整 run，signal_facts 记录折回值与因子。"""
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-01-09", 110.00, factor=2.0)
    conn.execute(
        "INSERT INTO indicators_daily (symbol, trade_date, ma20, computed_at) "
        "VALUES (?, '2026-01-09', 200.0, ?)",
        (SYM, db.utc_now()),
    )
    with conn:
        dw.run_daily_watch(conn, SYM)
    row = facts(conn, "ma_comparison")[-1]
    assert row[1] == "above"
    assert row[3]["mas"]["ma20"]["adjusted"] == 200.0
    assert row[3]["mas"]["ma20"]["raw_equiv"] == pytest.approx(100.0)
    assert row[3]["price_adj_factor"] == 2.0
    conn.close()


# ---------------------------------------------------------------- 箱体位置

def test_box_position_categories():
    box = {"box_low": Decimal("48"), "box_high": Decimal("65"),
           "buy_zone_low": Decimal("52"), "buy_zone_high": Decimal("56"),
           "sell_zone_low": Decimal("62"), "sell_zone_high": Decimal("65"),
           "box_invalidation": Decimal("46")}
    cases = [("45", "box_breached", 1), ("66", "above_box", 0),
             ("63", "sell_zone", 1), ("54", "buy_zone", 1),
             ("47", "below_box", 0), ("58", "mid_box", 0)]
    for close, pos, trig in cases:
        det = dw.box_position_state(Decimal(close), box)
        assert (det["state"], det["triggered"]) == (pos, trig), close


# ---------------------------------------------------------------- 无卡片 / 冻结（§2.5、§5.4b）

def test_no_active_card_incomplete(tmp_path):
    conn = make_conn(tmp_path)
    add_bar(conn, "2026-01-09", 55.00)
    with conn:
        res = dw.run_daily_watch(conn, SYM)
    assert (res.status, res.reason) == ("incomplete", "no_active_card")
    rows = facts(conn, "daily_watch")
    assert len(rows) == 1 and rows[0][1] == "incomplete"
    assert rows[0][3]["reason"] == "no_active_card"
    # 卡片相关信号一律不产出
    for sig in ("tier_proximity", "tier_triggered", "falsification_breach"):
        assert facts(conn, sig) == []
    conn.close()


def test_corporate_action_freeze_suspends_triggers(tmp_path):
    """冻结后（ex_date 起）卡片触发挂起：只写 daily_watch suspended 行。"""
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-01-08", 55.00)
    add_bar(conn, "2026-01-09", 55.00)
    conn.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio,
            source, created_at)
        VALUES (?, '2026-01-09', 'bonus_share', '2.0', 'test', ?)
        """,
        (SYM, db.utc_now()),
    )
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-01-09")
        assert len(res.frozen_drafts) == 1
        dw.run_daily_watch(conn, SYM)
    # 冻结日（01-09）无档位行，只有 suspended；冻结前一日（01-08）正常
    assert [r[0] for r in facts(conn, "tier_triggered")] == ["2026-01-08"]
    susp = [r for r in facts(conn, "daily_watch") if r[1] == "suspended"]
    assert [r[0] for r in susp] == ["2026-01-09"]
    assert susp[0][3]["suspensions"][0]["action_type"] == "bonus_share"
    conn.close()


# ---------------------------------------------------------------- 右侧确认状态机（§5.4）

def _day(d, close, low, vol=250.0, base=100.0):
    return {"trade_date": d, "close": Decimal(str(close)),
            "low": Decimal(str(low)), "volume_adj": vol, "vol_base": base}


def test_right_side_confirmed_path():
    """idle →（放量突破）waiting_retest →（回踩不破）confirmed；无止损位直接回 idle。"""
    days = [_day("2026-01-05", 102, 101), _day("2026-01-06", 100.5, 101.0)]
    trans, tracks, final = rs.evaluate_segment(days, Decimal("100"), P["right_side"])
    assert [(t["from_state"], t["to_state"]) for t in trans] == [
        ("idle", "waiting_retest"), ("waiting_retest", "confirmed")]
    assert tracks == [] and final == "idle"  # 无 stop_level：confirmed 后回 idle
    assert trans[1]["tracking"] == "no_stop_level"
    t0 = trans[0]
    assert t0["trigger_level"] == "100"
    assert t0["volume_adj"] == 250.0 and t0["vol_base"] == 100.0
    assert t0["volume_ratio"] == pytest.approx(2.5)


def test_right_side_invalidated_path():
    """等待期间收盘 ≤ 关键位×0.99 → invalidated（恰好 1% 也算，≤ 锁定）。"""
    for close, expect in [(99.00, "invalidated"), (98.99, "invalidated"),
                          (99.01, None)]:
        days = [_day("2026-01-05", 102, 101), _day("2026-01-06", close, 102.5)]
        trans, _, _ = rs.evaluate_segment(days, Decimal("100"), P["right_side"])
        kinds = [t["to_state"] for t in trans]
        assert (kinds[-1] if len(kinds) > 1 else None) == expect, close


def test_right_side_expired_path():
    """10 个交易日内无合格回踩 → 第 10 日 expired；第 9 日仍等待。"""
    days = [_day("2026-01-05", 102, 101)]
    days += [_day(f"2026-01-{6 + k:02d}", 101.5, 102.5) for k in range(9)]
    trans, _, _ = rs.evaluate_segment(days, Decimal("100"), P["right_side"])
    assert [t["to_state"] for t in trans] == ["waiting_retest"]
    days.append(_day("2026-01-15", 101.5, 102.5))  # 第 10 个等待日
    trans, _, _ = rs.evaluate_segment(days, Decimal("100"), P["right_side"])
    assert [t["to_state"] for t in trans] == ["waiting_retest", "expired"]
    assert trans[-1]["days_waited"] == 10


def test_right_side_breakout_boundaries():
    """突破边界：收盘恰好 +1% 触发（≥）；量恰好 vol_multiple 倍触发；量不足/样本不足不触发。"""
    lvl = Decimal("100")
    # 收盘 101.00 恰好 +1% → 突破；100.99 不突破
    for close, expect in [(101.00, True), (100.99, False)]:
        trans, _, _ = rs.evaluate_segment([_day("d1", close, close)], lvl,
                                          P["right_side"])
        assert bool(trans) is expect, close
    # 量边界从配置推导：恰好 vol_multiple 倍 → 突破；差 0.01 不突破
    thr = 100.0 * float(P["right_side"]["vol_multiple"])
    for vol, expect in [(thr, True), (thr - 0.01, False)]:
        trans, _, _ = rs.evaluate_segment(
            [_day("d1", 102, 101, vol=vol)], lvl, P["right_side"])
        assert bool(trans) is expect, vol
    # 量/价任一不满足、均量样本不足 → 不突破
    assert rs.evaluate_segment([_day("d1", 102, 101, vol=thr - 5.0)], lvl,
                               P["right_side"])[0] == []
    assert rs.evaluate_segment([_day("d1", 105, 104, base=None)], lvl,
                               P["right_side"])[0] == []


def test_right_side_holding_track_and_stopped_out():
    """confirmed 后（卡有止损位）→ holding 逐日跟踪；收盘 ≤ 止损位 → stopped_out（≤ 锁定）。"""
    p = P["right_side"]
    stop = Decimal("95")
    days = [_day("2026-01-05", 102, 101),      # 放量突破
            _day("2026-01-06", 100.5, 101.0),  # confirmed
            _day("2026-01-07", 100.2, 99.8),   # holding 第 1 日
            _day("2026-01-08", 96.0, 95.5)]    # holding 第 2 日（>95 不止损）
    trans, tracks, final = rs.evaluate_segment(days, Decimal("100"), p, stop=stop)
    assert [t["to_state"] for t in trans] == ["waiting_retest", "confirmed"]
    assert trans[1]["tracking"] == "holding"
    assert [(r["observed_on"], r["to_state"], r["days_since_confirm"])
            for r in tracks] == [("2026-01-07", "holding", 1),
                                 ("2026-01-08", "holding", 2)]
    assert tracks[0]["stop_level"] == "95" and final == "holding"
    # 恰好收在止损位 → stopped_out（≤ 锁定）；高 0.01 → 仍 holding
    days.append(_day("2026-01-09", 95.0, 94.5))
    trans, tracks, final = rs.evaluate_segment(days, Decimal("100"), p, stop=stop)
    assert trans[-1]["to_state"] == "stopped_out"
    assert trans[-1]["reason"] == "close_below_stop_level"
    assert trans[-1]["confirmed_on"] == "2026-01-06" and final == "idle"
    days[-1] = _day("2026-01-09", 95.01, 94.5)
    trans, tracks, final = rs.evaluate_segment(days, Decimal("100"), p, stop=stop)
    assert [t["to_state"] for t in trans] == ["waiting_retest", "confirmed"]
    assert len(tracks) == 3 and final == "holding"


def test_right_side_states_always_reset_not_silently_held():
    """结构性护栏：非 idle/waiting_retest/holding 状态抛 ValueError 而非静默按
    holding 处理（防未来重构引入未归位状态被 else 吞掉）。

    状态机只由局部 state 驱动、合法转换不可能产生非法状态，故本测试锁定可观察
    不变式：stopped_out 后必须回到 idle 才能开启新一轮 episode，绝不滞留疑似
    holding 的模糊状态。
    """
    p = P["right_side"]
    stop = Decimal("95")
    days = [
        _day("2026-01-05", 102, 101),            # ep1 放量突破
        _day("2026-01-06", 100.5, 101.0),        # ep1 confirmed → holding
        _day("2026-01-07", 100.0, 99.5),         # holding 跟踪
        _day("2026-01-08", 94.9, 94.5),          # 破止损 → stopped_out → idle
        _day("2026-01-09", 103, 102, vol=250.0),  # ep2 新一轮突破（要求 idle 已归位）
        _day("2026-01-12", 100.5, 101.0),        # ep2 confirmed → holding
    ]
    trans, tracks, final = rs.evaluate_segment(days, Decimal("100"), p, stop=stop)
    assert [t["to_state"] for t in trans] == [
        "waiting_retest", "confirmed", "stopped_out", "waiting_retest", "confirmed"]
    assert [r["observed_on"] for r in tracks] == ["2026-01-07"]
    assert final == "holding"


def test_right_side_integration(tmp_path):
    """真实库跑通：20 日均量基数（shift(1)）+ 突破 + 回踩 confirmed 入 signal_facts。"""
    conn = make_conn(tmp_path)
    add_card(conn, trigger=("100.00", "95.00"))
    # 20 个基数日量 100、价 95；随后突破日 + 回踩日
    for k in range(20):
        add_bar(conn, f"2026-01-{5 + k:02d}" if k < 25 else f"2026-02-0{k - 24}",
                95.0, volume=100.0)
    add_bar(conn, "2026-01-30", 102.0, low=101.0, volume=250.0)
    add_bar(conn, "2026-02-02", 100.5, low=101.0, volume=120.0)
    with conn:
        res = rs.run_right_side(conn, SYM)
    assert res.status == "ok"
    assert res.current_state == "holding"  # 卡有 stop_level：confirmed 后转跟踪
    rows = facts(conn, "right_side")
    assert [(r[0], r[1]) for r in rows] == [
        ("2026-01-30", "waiting_retest"), ("2026-02-02", "confirmed")]
    assert rows[0][3]["vol_base"] == pytest.approx(100.0)
    assert rows[0][3]["trigger_level"] == "100.00"
    conn.close()


def test_right_side_holding_integration(tmp_path):
    """真实库：confirmed 后逐日落 holding 行（triggered=0），破止损位落 stopped_out（triggered=1）。"""
    conn = make_conn(tmp_path)
    add_card(conn, trigger=("100.00", "95.00"))
    for k in range(20):
        add_bar(conn, f"2026-01-{5 + k:02d}", 95.0, volume=100.0)   # 01-05..01-24
    add_bar(conn, "2026-01-30", 102.0, low=101.0, volume=250.0)    # 突破
    add_bar(conn, "2026-02-02", 100.5, low=101.0, volume=120.0)    # confirmed
    add_bar(conn, "2026-02-03", 100.2, low=99.8, volume=120.0)     # holding
    add_bar(conn, "2026-02-04", 94.9, low=94.5, volume=120.0)      # 破止损位
    with conn:
        res = rs.run_right_side(conn, SYM)
    assert res.status == "ok" and res.current_state == "idle"
    rows = facts(conn, "right_side")
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("2026-01-30", "waiting_retest", 1),
        ("2026-02-02", "confirmed", 1),
        ("2026-02-03", "holding", 0),
        ("2026-02-04", "stopped_out", 1)]
    assert rows[2][3]["stop_level"] == "95.00"
    assert rows[3][3]["confirmed_on"] == "2026-02-02"
    conn.close()


def test_right_side_as_of_gap_uses_effective_window(tmp_path):
    """新旧卡交替空档：旧卡 superseded 但窗口仍覆盖 as_of → 照常计算（§5.1 窗口语义，
    不用 load_active_card 的 status 口径，否则误报 no_active_card）。"""
    conn = make_conn(tmp_path)
    add_card(conn, card_id="cv_old", trigger=("100.00", "95.00"), status="superseded")
    conn.execute("UPDATE strategy_card_versions SET effective_to = '2026-03-01' "
                 "WHERE card_version_id = 'cv_old'")
    add_card(conn, card_id="cv_new", eff_from="2026-03-01",
             trigger=("110.00", "100.00"))
    for k in range(20):
        add_bar(conn, f"2026-01-{5 + k:02d}", 95.0, volume=100.0)
    with conn:
        res = rs.run_right_side(conn, SYM, as_of="2026-01-24")
    assert res.status == "ok"
    run = conn.execute("SELECT card_version_id, status FROM pipeline_runs "
                       "WHERE run_id = ?", (res.run_id,)).fetchone()
    assert run["card_version_id"] == "cv_old" and run["status"] == "success"
    conn.close()


def test_right_side_no_card_incomplete(tmp_path):
    conn = make_conn(tmp_path)
    add_bar(conn, "2026-01-09", 100.0)
    with conn:
        res = rs.run_right_side(conn, SYM)
    assert (res.status, res.reason) == ("incomplete", "no_active_card")
    assert res.current_state == "idle"
    rows = facts(conn, "right_side")
    assert len(rows) == 1 and rows[0][1] == "idle"
    assert rows[0][3]["reason"] == "no_active_card"
    conn.close()


# ---------------------------------------------------------------- 缺失数据（§2.5）

def test_daily_watch_as_of_before_earliest_bar(tmp_path):
    """as_of 早于最早 bar：不删旧 facts，直接写 incomplete 行返回（§2.5 不猜）。"""
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-01-09", 55.00)
    with conn:
        res = dw.run_daily_watch(conn, SYM)
    assert res.status == "ok"
    assert len(facts(conn, "falsification_breach")) == 1  # 旧 facts 已存在
    with conn:
        res = dw.run_daily_watch(conn, SYM, as_of="2026-01-01")
    assert (res.status, res.reason) == ("incomplete", "no_bars_on_or_before_as_of")
    # 旧 facts 未被 DELETE 清掉
    assert len(facts(conn, "falsification_breach")) == 1
    # 只新增 as_of 当日的 incomplete 行
    rows = facts(conn, "daily_watch")
    assert [(r[0], r[1]) for r in rows] == [("2026-01-01", "incomplete")]
    assert rows[0][3]["reason"] == "no_bars_on_or_before_as_of"
    conn.close()


def test_daily_watch_null_close_bar_skipped(tmp_path):
    """close_raw 为 NULL 的 bar：记 incomplete 跳过，不崩溃不猜（§2.5）。"""
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-01-08", 55.00)
    add_null_bar(conn, "2026-01-09", low=54.0, volume=100.0)  # close_raw NULL
    add_bar(conn, "2026-01-12", 55.00)
    with conn:
        res = dw.run_daily_watch(conn, SYM)
    assert res.status == "ok"
    inc = [r for r in facts(conn, "daily_watch") if r[1] == "incomplete"]
    assert [(r[0], r[3]["reason"]) for r in inc] == [
        ("2026-01-09", "missing_close_raw")]
    # 正常日仍产出卡片信号，NULL 日不产出
    assert [r[0] for r in facts(conn, "tier_triggered")] == [
        "2026-01-08", "2026-01-12"]
    conn.close()


def test_daily_watch_as_of_gap_uses_effective_window(tmp_path):
    """新旧卡交替空档：旧卡 superseded 但窗口覆盖 as_of → 照常产出，不写
    incomplete 汇总行（§5.1 窗口语义，与逐日循环口径一致）。"""
    conn = make_conn(tmp_path)
    add_card(conn, card_id="cv_old", status="superseded")
    conn.execute("UPDATE strategy_card_versions SET effective_to = '2026-03-01' "
                 "WHERE card_version_id = 'cv_old'")
    add_card(conn, card_id="cv_new", eff_from="2026-03-01")
    add_bar(conn, "2026-01-09", 55.00)
    with conn:
        res = dw.run_daily_watch(conn, SYM, as_of="2026-01-09")
    assert res.status == "ok"
    assert all(r[1] != "incomplete" for r in facts(conn, "daily_watch"))
    conn.close()


def test_right_side_null_ohlcv_bars_skipped(tmp_path):
    """NULL close/volume 的 bar：不当 0、不进 Decimal，记 incomplete 跳过（§2.5）。"""
    conn = make_conn(tmp_path)
    add_card(conn, trigger=("100.00", "95.00"))
    add_bar(conn, "2026-01-05", 95.0, volume=100.0)
    add_null_bar(conn, "2026-01-06", close=95.0, low=94.0)      # volume_raw NULL
    add_null_bar(conn, "2026-01-07", low=94.0, volume=100.0)    # close_raw NULL
    add_bar(conn, "2026-01-08", 95.0, volume=100.0)
    with conn:
        res = rs.run_right_side(conn, SYM)
    assert (res.status, res.reason) == ("incomplete", "missing_ohlcv_bars")
    assert res.current_state == "idle"
    assert any("缺失" in n for n in res.notes)
    assert facts(conn, "right_side") == []  # 缺失 bar 不参与判定，无转换
    conn.close()
