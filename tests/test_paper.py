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
        """,
        (symbol, td, close, close * 1.02, close * 0.98, close, factor,
         turnover, db_utc_now()))


def add_fact(conn, symbol, td, signal, state, triggered=0, details="{}"):
    conn.execute(
        """
        INSERT INTO signal_facts (symbol, observed_on, signal, state, triggered,
            details_json, run_id, rule_version, config_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'test', 'signals_v2', 'h', ?)
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
