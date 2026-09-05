"""模拟盘测试：决策点枚举（事件化/去抖/结构性skip）、录入窗口、结算、统计、报告段。"""
import json
from pathlib import Path

import pytest

from scripts.pipeline import db as pdb
from scripts.paper import common as pc
from scripts.paper import decide as pd
from scripts.paper import settle as ps
from scripts.paper import stats as pst


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(tmp_path / "m.db")
    pdb.migrate(c)
    pdb.seed(c)  # watchlist 34 只 + CN 2023-2026 日历
    yield c
    c.close()


CFG = {"notional_per_trade": 100000, "max_hold_days": 60,
       "decision_window_days": 1, "box_entry_debounce_days": 5,
       "deep_exit_pct": 0.05, "baseline": "naive_follow_all"}


def add_bar(conn, symbol, td, close, factor=1.0, turnover=None):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, amount_raw, currency,
            price_adj_factor, share_factor, trading_status, turnover,
            source, updated_at)
        VALUES (?, ?, 'CN', ?, ?, ?, ?, 1000000, 5000000, 'CNY', ?, 1.0,
                'normal', ?, 'test', ?)
        ON CONFLICT(symbol, trade_date) DO UPDATE SET close_raw=excluded.close_raw
        """,
        (symbol, td, close, close * 1.02, close * 0.98, close, factor,
         turnover, db_utc_now()))


def add_fact(conn, symbol, td, signal, state, triggered=0, details="{}"):
    conn.execute(
        """
        INSERT INTO signal_facts (symbol, observed_on, signal, state, triggered,
            details_json, run_id, rule_version, config_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'test', 'signals_v2', 'h', ?)
        ON CONFLICT(symbol, signal, observed_on) DO NOTHING
        """,
        (symbol, td, signal, state, triggered, details, db_utc_now()))


def db_utc_now():
    return pdb.utc_now()


def open_pos(conn, symbol, entry_date, close, deep_line):
    conn.execute(
        """
        INSERT INTO paper_decisions (symbol, decision_date, decision_type,
            signal_source, signal_snapshot_json, decision, close_used,
            notional, late, run_id, created_at)
        VALUES (?, ?, 'entry', 'tier_triggered', '{}', 'follow', ?, 100000,
                0, 'test', ?)
        """,
        (symbol, entry_date, str(close), db_utc_now()))
    did = conn.execute("SELECT MAX(id) FROM paper_decisions").fetchone()[0]
    conn.execute(
        """
        INSERT INTO paper_positions (symbol, entry_decision_id, entry_date,
            entry_close, quantity, notional, deep_exit_line, status)
        VALUES (?, ?, ?, ?, 1000, 100000, ?, 'open')
        """,
        (symbol, did, entry_date, str(close), str(deep_line)))


# ---------------------------------------------------------------- 枚举


class TestEnumerate:
    def test_tier_transition_only_first_day(self, conn):
        for i, td in enumerate(["2026-09-01", "2026-09-02", "2026-09-03"]):
            add_bar(conn, "603605.SH", td, 62.0 + i)
        add_fact(conn, "603605.SH", "2026-09-01", "tier_triggered", "inactive")
        add_fact(conn, "603605.SH", "2026-09-02", "tier_triggered", "triggered", 1)
        add_fact(conn, "603605.SH", "2026-09-03", "tier_triggered", "triggered", 1)
        conn.commit()
        pts = pc.enumerate_decision_points(conn, "2026-09-03", CFG)
        tiers = [p for p in pts if p["signal_source"] == "tier_triggered"]
        assert len(tiers) == 1 and tiers[0]["decision_date"] == "2026-09-02"

    def test_falsification_watch_not_event_confirm_day_is(self, conn):
        for i, td in enumerate(["2026-09-04", "2026-09-07"]):
            add_bar(conn, "603605.SH", td, 61.0)
        add_fact(conn, "603605.SH", "2026-09-04", "falsification_breach",
                 "watch", 0)  # watch 态 breached_today=true 也不产点（triggered=0）
        add_fact(conn, "603605.SH", "2026-09-07", "falsification_breach",
                 "active", 1)  # 确认日
        conn.commit()
        pts = pc.enumerate_decision_points(conn, "2026-09-07", CFG)
        fb = [p for p in pts if p["signal_source"] == "falsification_breach"]
        assert len(fb) == 1 and fb[0]["decision_date"] == "2026-09-07"

    def test_box_entry_debounce(self, conn):
        tds = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
               "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10",
               "2026-09-14", "2026-09-15"]
        for td in tds:
            add_bar(conn, "603288.SH", td, 34.0)
        states = ["mid_box", "buy_zone", "buy_zone", "sell_zone", "mid_box",
                  "buy_zone", "mid_box", "mid_box", "buy_zone", "buy_zone"]
        for td, st in zip(tds, states):
            add_fact(conn, "603288.SH", td, "box_position", st)
        conn.commit()
        pts = [p for p in pc.enumerate_decision_points(conn, "2026-09-15", CFG)
               if p["signal_source"] == "box_entry"]
        # 09-02 首次进 buy_zone → 点；09-08 距首点 4 个交易日（冷却内）→ 无点；
        # 09-14 距 09-02 有 8 个交易日 → 新点
        assert [p["decision_date"] for p in pts] == ["2026-09-02", "2026-09-14"]

    def test_deep_exit_first_cross(self, conn):
        for td, c in [("2026-09-01", 10.0), ("2026-09-02", 9.8),
                      ("2026-09-03", 9.4)]:
            add_bar(conn, "600115.SH", td, c)
        open_pos(conn, "600115.SH", "2026-09-01", 10.0, 9.5)
        conn.commit()
        pts = [p for p in pc.enumerate_decision_points(conn, "2026-09-03", CFG)
               if p["signal_source"] == "deep_exit"]
        assert len(pts) == 1 and pts[0]["decision_date"] == "2026-09-03"

    def test_already_decided_not_relisted(self, conn):
        add_bar(conn, "603605.SH", "2026-09-02", 62.0)
        add_fact(conn, "603605.SH", "2026-09-02", "tier_triggered", "triggered", 1)
        conn.execute(
            """
            INSERT INTO paper_decisions (symbol, decision_date, decision_type,
                signal_source, signal_snapshot_json, decision, close_used,
                notional, late, run_id, created_at)
            VALUES ('603605.SH', '2026-09-02', 'entry', 'tier_triggered',
                    '{}', 'follow', '62.0', 100000, 0, 'test', ?)
            """,
            (db_utc_now(),))
        conn.commit()
        pts = [p for p in pc.enumerate_decision_points(conn, "2026-09-02", CFG)
               if p["signal_source"] == "tier_triggered"]
        assert pts == []

    def test_single_position_constraint_tag(self, conn):
        for i, td in enumerate(["2026-09-01", "2026-09-02", "2026-09-03"]):
            add_bar(conn, "603605.SH", td, 62.0 + i)
        add_fact(conn, "603605.SH", "2026-09-03", "tier_triggered", "triggered", 1)
        open_pos(conn, "603605.SH", "2026-09-01", 62.0, 55.0)
        conn.commit()
        pts = [p for p in pc.enumerate_decision_points(conn, "2026-09-03", CFG)
               if p["signal_source"] == "tier_triggered"]
        assert len(pts) == 1
        assert pts[0]["constraint_tag"] == "single_position"


# ---------------------------------------------------------------- 录入


def _mk_point(conn, symbol="603605.SH", date="2026-09-02", dtype="entry",
              source="tier_triggered", close="62.0", constraint=None):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, amount_raw, currency,
            price_adj_factor, share_factor, trading_status, turnover,
            source, updated_at)
        VALUES (?, ?, 'CN', ?, ?, ?, ?, 1000000, 5000000, 'CNY', 1.0, 1.0,
                'normal', NULL, 'test', ?)
        ON CONFLICT(symbol, trade_date) DO NOTHING
        """,
        (symbol, date, close, float(close) * 1.02, float(close) * 0.98,
         close, db_utc_now()))
    if source == "tier_triggered":
        add_fact(conn, symbol, date, source, "triggered", 1)
    elif source == "falsification_breach":
        add_fact(conn, symbol, date, source, "active", 1)
    elif source == "right_side":
        add_fact(conn, symbol, date, source, "confirmed", 1)
    conn.commit()
    pts = pc.enumerate_decision_points(conn, date, CFG)
    pts = [p for p in pts if p["symbol"] == symbol
           and p["decision_type"] == dtype]
    return pts[0] if pts else None


class TestDecide:
    def test_follow_entry_creates_position_lots(self, conn):
        pt = _mk_point(conn)  # close 62.0 → 100000//62=1612 → 1600 股
        assert pt is not None
        with conn:
            did = pd.record_decision(conn, pt, "follow", "", 
                                     "2026-09-02T21:00:00+08:00", CFG)
        dec = conn.execute("SELECT * FROM paper_decisions").fetchone()
        assert dec["quantity"] == 1600 and dec["late"] == 0
        pos = conn.execute("SELECT * FROM paper_positions").fetchone()
        assert pos["status"] == "open"
        assert float(pos["deep_exit_line"]) == pytest.approx(62.0 * 0.95,
                                                             abs=1e-3)

    def test_counter_on_exit_rejected(self, conn):
        pt = _mk_point(conn, dtype="exit", source="falsification_breach",
                       close="61.0")
        if pt is None:  # exit 点需要 open position 才枚举
            open_pos(conn, "603605.SH", "2026-09-01", 62.0, 55.0)
            conn.commit()
            pt = _mk_point(conn, dtype="exit", source="falsification_breach",
                           close="61.0")
        with pytest.raises(ValueError, match="exit 决策点"):
            pd.record_decision(conn, pt, "counter", "",
                               "2026-09-02T21:00:00+08:00", CFG)

    def test_single_position_rejects_follow_skip_tagged(self, conn):
        pt = _mk_point(conn, date="2026-09-03", close="62.5")
        open_pos(conn, "603605.SH", "2026-09-01", 62.0, 55.0)
        conn.commit()
        pt = _mk_point(conn, date="2026-09-03", close="62.5")
        assert pt and pt["constraint_tag"] == "single_position"
        with pytest.raises(ValueError, match="一股一仓"):
            pd.record_decision(conn, pt, "follow", "",
                               "2026-09-03T21:00:00+08:00", CFG)
        with conn:
            pd.record_decision(conn, pt, "skip", "",
                               "2026-09-03T21:00:00+08:00", CFG)
        dec = conn.execute(
            "SELECT * FROM paper_decisions WHERE decision='skip'").fetchone()
        assert dec["constraint_tag"] == "single_position"
        assert dec["decision"] == "skip"

    def test_late_flag_t_plus_2(self, conn):
        pt = _mk_point(conn, date="2026-09-01", close="62.0")
        # 决策日 09-01（周二），now=09-03（周四，T+2 交易日）→ late
        with conn:
            pd.record_decision(conn, pt, "skip", "",
                               "2026-09-03T21:00:00+08:00", CFG)
        dec = conn.execute("SELECT late FROM paper_decisions").fetchone()
        assert dec["late"] == 1

    def test_duplicate_rejected(self, conn):
        pt = _mk_point(conn)
        with conn:
            pd.record_decision(conn, pt, "follow", "",
                               "2026-09-02T21:00:00+08:00", CFG)
        with pytest.raises(ValueError, match="一点一决"):
            pd.record_decision(conn, pt, "skip", "",
                               "2026-09-02T21:00:00+08:00", CFG)

    def test_exit_follow_closes_position(self, conn):
        add_bar(conn, "603605.SH", "2026-09-02", 62.0, factor=1.0)
        add_bar(conn, "603605.SH", "2026-09-03", 60.0, factor=1.0)
        add_fact(conn, "603605.SH", "2026-09-03", "falsification_breach",
                 "active", 1)
        open_pos(conn, "603605.SH", "2026-09-02", 62.0, 55.0)
        conn.commit()
        pt = _mk_point(conn, date="2026-09-03", dtype="exit",
                       source="falsification_breach", close="60.0")
        assert pt is not None
        with conn:
            pd.record_decision(conn, pt, "follow", "",
                               "2026-09-03T21:00:00+08:00", CFG)
        pos = conn.execute("SELECT * FROM paper_positions").fetchone()
        assert pos["status"] == "closed"
        assert pos["exit_source"] == "falsification_breach"
        # ret = 60/62 - 1（同因子）
        assert pos["ret"] == pytest.approx(60.0 / 62.0 - 1, abs=1e-9)
        assert pos["pnl"] == pytest.approx(100000 * (60.0 / 62.0 - 1), abs=1e-6)
        assert pos["hold_days"] == 2


# ---------------------------------------------------------------- 结算


def _entry_follow(conn, symbol, date, close, factor=1.0, deep_line=None):
    pt = _mk_point(conn, symbol=symbol, date=date, close=str(close))
    if pt is None:
        pt = _mk_point(conn, symbol=symbol, date=date, close=str(close))
    with conn:
        did = pd.record_decision(conn, pt, "follow", "",
                                 f"{date}T21:00:00+08:00", CFG)
    if deep_line is not None:
        conn.execute("UPDATE paper_positions SET deep_exit_line=? "
                     "WHERE symbol=?", (str(deep_line), symbol))
    return did


class TestSettle:
    def test_exdiv_factor_ret(self, conn):
        # 送股除权：entry 62@1.0 → exit 30@2.0 → 复权 ret = 60/62-1（非 -51%）
        add_bar(conn, "603605.SH", "2026-09-01", 62.0, factor=1.0)
        add_bar(conn, "603605.SH", "2026-09-02", 30.0, factor=2.0)
        add_fact(conn, "603605.SH", "2026-09-02", "falsification_breach",
                 "active", 1)
        open_pos(conn, "603605.SH", "2026-09-01", 62.0, 20.0)
        conn.commit()
        pt = _mk_point(conn, date="2026-09-02", dtype="exit",
                       source="falsification_breach", close="30.0")
        with conn:
            pd.record_decision(conn, pt, "follow", "",
                               "2026-09-02T21:00:00+08:00", CFG)
        pos = conn.execute("SELECT * FROM paper_positions").fetchone()
        assert pos["ret"] == pytest.approx(60.0 / 62.0 - 1, abs=1e-9)

    def test_timeout_with_suspension_deferral(self, conn):
        cfg = dict(CFG, max_hold_days=5)
        # entry 09-01（周二）；09-02 有 bar；09-03/09-04 停牌无 bar；09-07 复牌
        for td, c in [("2026-09-01", 62.0), ("2026-09-02", 62.0),
                      ("2026-09-07", 61.0), ("2026-09-08", 61.5)]:
            add_bar(conn, "603605.SH", td, c)
        pt = _mk_point(conn, date="2026-09-01", close="62.0")
        with conn:
            pd.record_decision(conn, pt, "follow", "",
                               "2026-09-01T21:00:00+08:00", cfg)
        # now=09-05（周六）：5 个交易日目标日=09-07，但 09-05 < 09-07 → 未到期
        settled = ps.run_settle(conn, cfg, "2026-09-05")
        assert settled == []
        # now=09-08：目标日 09-07 有 bar → 按 09-07 结算
        settled = ps.run_settle(conn, cfg, "2026-09-08")
        assert len(settled) == 1
        pos = conn.execute("SELECT * FROM paper_positions").fetchone()
        assert pos["status"] == "closed" and pos["exit_source"] == "timeout"
        assert pos["exit_date"] == "2026-09-07"
        assert pos["hold_days"] == 5
        # 已结算不重复
        assert ps.run_settle(conn, cfg, "2026-09-08") == []

    def test_timeout_suspension_pushes_exit_date(self, conn):
        cfg = dict(CFG, max_hold_days=3)
        # entry 09-01；目标日=09-03 停牌（无 bar）→ 顺延至下一有 bar 日 09-04
        for td, c in [("2026-09-01", 62.0), ("2026-09-02", 62.0),
                      ("2026-09-04", 61.0)]:
            add_bar(conn, "603605.SH", td, c)
        pt = _mk_point(conn, date="2026-09-01", close="62.0")
        with conn:
            pd.record_decision(conn, pt, "follow", "",
                               "2026-09-01T21:00:00+08:00", cfg)
        settled = ps.run_settle(conn, cfg, "2026-09-06")
        assert len(settled) == 1
        pos = conn.execute("SELECT * FROM paper_positions").fetchone()
        assert pos["exit_date"] == "2026-09-04"
        assert pos["hold_days"] == 4  # 09-01→09-04 含停牌（09-03）

    def test_manual_close(self, conn):
        for td, c in [("2026-09-01", 62.0), ("2026-09-02", 63.0)]:
            add_bar(conn, "603605.SH", td, c)
        _entry_follow(conn, "603605.SH", "2026-09-01", 62.0)
        with conn:
            ps.manual_close(conn, "603605.SH", "情绪化想卖，记录一下",
                            "2026-09-02", CFG, "2026-09-02T21:00:00+08:00")
        pos = conn.execute("SELECT * FROM paper_positions").fetchone()
        assert pos["status"] == "closed" and pos["exit_source"] == "manual"
        assert pos["ret"] == pytest.approx(63.0 / 62.0 - 1, abs=1e-9)
        dec = conn.execute(
            "SELECT * FROM paper_decisions WHERE signal_source='manual'"
        ).fetchone()
        assert "情绪化" in dec["note"]

    def test_reversal_closes_open_position(self, conn):
        for td, c in [("2026-09-01", 62.0), ("2026-09-02", 63.0)]:
            add_bar(conn, "603605.SH", td, c)
        did = _entry_follow(conn, "603605.SH", "2026-09-01", 62.0)
        with conn:
            rid = ps.reversal(conn, did, "录错了，其实是想 skip",
                              "2026-09-02", CFG, "2026-09-02T22:00:00+08:00")
        orig = conn.execute("SELECT * FROM paper_decisions WHERE id=?",
                            (did,)).fetchone()
        assert orig["reversed_by"] == rid
        pos = conn.execute("SELECT * FROM paper_positions").fetchone()
        assert pos["status"] == "closed" and pos["exit_source"] == "reversal"
        with pytest.raises(ValueError, match="已被冲正"):
            ps.reversal(conn, did, "再冲一次", "2026-09-02", CFG,
                        "2026-09-02T23:00:00+08:00")


# ---------------------------------------------------------------- 统计


class TestStats:
    def _scenario(self, conn):
        """point1(09-01 tier) follow；point2(09-03 right_side) skip；
        09-07 falsification 确认 → exit-follow 平仓。closes: 62,63,60,62,64。"""
        closes = {"2026-09-01": 62.0, "2026-09-02": 63.0, "2026-09-03": 60.0,
                  "2026-09-04": 62.0, "2026-09-07": 64.0}
        for td, c in closes.items():
            add_bar(conn, "603605.SH", td, c)
        add_fact(conn, "603605.SH", "2026-09-01", "tier_triggered", "triggered", 1)
        add_fact(conn, "603605.SH", "2026-09-03", "right_side", "confirmed", 1)
        add_fact(conn, "603605.SH", "2026-09-07", "falsification_breach",
                 "active", 1)
        conn.commit()
        pts = pc.enumerate_decision_points(conn, "2026-09-07", CFG)
        p1 = next(p for p in pts if p["signal_source"] == "tier_triggered")
        p2 = next(p for p in pts if p["signal_source"] == "right_side")
        with conn:
            pd.record_decision(conn, p1, "follow", "",
                               "2026-09-01T21:00:00+08:00", CFG)
            pd.record_decision(conn, p2, "skip", "",
                               "2026-09-03T21:00:00+08:00", CFG)
        pe = next(p for p in pc.enumerate_decision_points(conn, "2026-09-07", CFG)
                  if p["decision_type"] == "exit")
        with conn:
            pd.record_decision(conn, pe, "follow", "",
                               "2026-09-07T21:00:00+08:00", CFG)

    def test_follow_skip_baseline_diff(self, conn):
        self._scenario(conn)
        s = pst.compute_stats(conn, CFG)
        # follow：64/62-1 → +3.2258%，pnl +3225.8
        assert s["follow"]["n"] == 1
        assert s["follow"]["winrate"] == 1.0
        assert s["follow"]["cum_pnl"] == pytest.approx(100000 * (64 / 62 - 1),
                                                       abs=1)
        # skip point2：09-03 close 60 入 → 09-07 close 64 出 = +6.667%（错过盈利）
        assert s["skip"]["n"] == 1
        assert s["skip"]["missed_profit_pnl"] == pytest.approx(
            100000 * (64 / 60 - 1), abs=1)
        assert s["skip"]["structural_n"] == 0
        # 基线：三事件全跟——point1 64/62-1、point2 64/60-1、
        # falsification 确认日（09-07）entry 双角色当日 MTM = 0
        assert s["baseline"]["n"] == 3
        assert s["baseline"]["cum_pnl"] == pytest.approx(
            100000 * ((64 / 62 - 1) + (64 / 60 - 1)), abs=1)
        # 判断力差值 = 主观 − 基线 = −skip 掉的盈利
        assert s["judgement_diff"] == pytest.approx(
            -100000 * (64 / 60 - 1), abs=2)
        assert s["sample_warning"] is True
        assert s["late_count"] == 0

    def test_counter_correct_when_signal_fails(self, conn):
        closes = {"2026-09-01": 62.0, "2026-09-02": 63.0, "2026-09-03": 60.0,
                  "2026-09-04": 62.0, "2026-09-07": 58.0}
        for td, c in closes.items():
            add_bar(conn, "603605.SH", td, c)
        add_fact(conn, "603605.SH", "2026-09-01", "tier_triggered", "triggered", 1)
        add_fact(conn, "603605.SH", "2026-09-03", "right_side", "confirmed", 1)
        conn.commit()
        pts = pc.enumerate_decision_points(conn, "2026-09-07", CFG)
        p2 = next(p for p in pts if p["signal_source"] == "right_side")
        with conn:
            pd.record_decision(conn, p2, "counter", "",
                               "2026-09-03T21:00:00+08:00", CFG)
        s = pst.compute_stats(conn, CFG)
        # point2 虚拟：63 入（09-03）→ MTM 58（09-07，无退出事件）= -7.94% → 看反正确
        assert s["counter"]["n"] == 1 and s["counter"]["correct_n"] == 1

    def test_structural_skip_layered(self, conn):
        closes = {"2026-09-01": 62.0, "2026-09-02": 62.0, "2026-09-03": 62.5}
        for td, c in closes.items():
            add_bar(conn, "603605.SH", td, c)
        open_pos(conn, "603605.SH", "2026-09-01", 62.0, 55.0)
        add_fact(conn, "603605.SH", "2026-09-03", "tier_triggered", "triggered", 1)
        conn.commit()
        pt = next(p for p in pc.enumerate_decision_points(conn, "2026-09-03", CFG)
                  if p["signal_source"] == "tier_triggered")
        with conn:
            pd.record_decision(conn, pt, "skip", "",
                               "2026-09-03T21:00:00+08:00", CFG)
        s = pst.compute_stats(conn, CFG)
        assert s["skip"]["structural_n"] == 1
        assert s["skip"]["autonomous_n"] == 0
