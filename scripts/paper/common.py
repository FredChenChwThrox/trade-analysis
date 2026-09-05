"""模拟盘公共层：配置、价格/日历工具、决策点枚举（纯读，不写库）。"""
from __future__ import annotations

import bisect
import json
import sqlite3
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_PAPER_CONFIG = _ROOT / "config" / "paper.yaml"

_CONFIG_DEFAULTS = {
    "notional_per_trade": 100000,
    "max_hold_days": 60,
    "decision_window_days": 1,
    "box_entry_debounce_days": 5,
    "deep_exit_pct": 0.05,
    "baseline": "naive_follow_all",
}

ENTRY_SOURCES = ("tier_triggered", "right_side", "falsification_breach",
                 "box_entry")


def load_config(db_path=None) -> dict:
    """config/paper.yaml → 参数（缺文件/缺键用默认）。"""
    cfg = dict(_CONFIG_DEFAULTS)
    if _PAPER_CONFIG.exists():
        doc = yaml.safe_load(_PAPER_CONFIG.read_text(encoding="utf-8")) or {}
        cfg.update(doc)
    return cfg


# ---------------------------------------------------------------- 日历/价格工具


def _cal_rows(conn: sqlite3.Connection) -> list[str]:
    """CN 市场开市交易日序列（升序，含停牌日——停牌占用交易日历，设计 §2.4）。"""
    return [r[0] for r in conn.execute(
        "SELECT trade_date FROM trading_calendar "
        "WHERE market='CN' AND is_open=1 ORDER BY trade_date")]


def trading_days_between(conn: sqlite3.Connection, d1: str, d2: str) -> int:
    """d1（含）→ d2（含）之间的交易日数；d2<d1 返回 0。"""
    days = _cal_rows(conn)
    i1 = bisect.bisect_left(days, d1)
    i2 = bisect.bisect_right(days, d2)
    return max(0, i2 - i1)


def next_trading_day(conn: sqlite3.Connection, d: str) -> str | None:
    days = _cal_rows(conn)
    i = bisect.bisect_right(days, d)
    return days[i] if i < len(days) else None


def is_late(conn: sqlite3.Connection, decision_date: str, now_date: str,
            window_days: int) -> bool:
    """now_date 距 decision_date 超过 window_days 个交易日 → late（§2.3 防线 2）。"""
    return trading_days_between(conn, decision_date, now_date) > window_days


def get_bar(conn: sqlite3.Connection, symbol: str, date: str):
    return conn.execute(
        "SELECT trade_date, close_raw, price_adj_factor, trading_status "
        "FROM daily_bars WHERE symbol=? AND trade_date=?",
        (symbol, date)).fetchone()


def next_bar_date(conn: sqlite3.Connection, symbol: str, after_date: str):
    """after_date 之后首个有 bar 的交易日（停牌顺延用，§2.4）。"""
    row = conn.execute(
        "SELECT MIN(trade_date) FROM daily_bars WHERE symbol=? AND trade_date>?",
        (symbol, after_date)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- 决策点枚举


def _signal_rows(conn: sqlite3.Connection, symbol: str, signal: str, as_of: str):
    return conn.execute(
        "SELECT observed_on, state, triggered, details_json FROM signal_facts "
        "WHERE symbol=? AND signal=? AND observed_on<=? ORDER BY observed_on",
        (symbol, signal, as_of)).fetchall()


def _decided(conn: sqlite3.Connection, symbol: str, date: str,
             dtype: str, source: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM paper_decisions WHERE symbol=? AND decision_date=? "
        "AND decision_type=? AND signal_source=?",
        (symbol, date, dtype, source)).fetchone() is not None


def _snap(conn: sqlite3.Connection, symbol: str, date: str,
          signal: str, state: str, triggered: int, details) -> str:
    bar = get_bar(conn, symbol, date)
    return json.dumps({
        "signal": signal, "state": state, "triggered": triggered,
        "details_json": details, "close_raw": bar["close_raw"] if bar else None,
        "price_adj_factor": bar["price_adj_factor"] if bar else None,
    }, ensure_ascii=False, sort_keys=True)


def enumerate_decision_points(conn: sqlite3.Connection, as_of: str,
                              cfg: dict) -> list[dict]:
    """扫描 signal_facts 生成待决策清单（纯读；已决/超出去重不列）。

    返回按 (decision_date, symbol, decision_type, signal_source) 排序的 dict 列表，
    pick_id 为 1 起展示序号（decide --pick 用）。
    """
    debounce = int(cfg.get("box_entry_debounce_days", 5))
    open_syms = {r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM paper_positions WHERE status='open'")}
    points: list[dict] = []

    def add(symbol, date, dtype, source, state, triggered, details):
        bar = get_bar(conn, symbol, date)
        if bar is None or bar["close_raw"] is None:
            return  # 信号日必有 bar（§2.4）；防御跳过
        points.append({
            "symbol": symbol, "decision_date": date, "decision_type": dtype,
            "signal_source": source,
            "signal_snapshot_json": _snap(conn, symbol, date, source, state,
                                          triggered, details),
            "close_used": str(bar["close_raw"]),
            "constraint_tag": ("single_position"
                               if dtype == "entry" and symbol in open_syms else None),
        })

    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM watchlist WHERE active=1 ORDER BY symbol")]
    for sym in symbols:
        # ---- entry: tier_triggered（→triggered 转变日；前一条存在行即"前值"）
        prev_state = None
        for r in _signal_rows(conn, sym, "tier_triggered", as_of):
            if r["state"] == "triggered" and prev_state != "triggered":
                if not _decided(conn, sym, r["observed_on"], "entry",
                                "tier_triggered"):
                    add(sym, r["observed_on"], "entry", "tier_triggered",
                        r["state"], r["triggered"], r["details_json"])
            prev_state = r["state"] if r["state"] is not None else prev_state
        # ---- entry: right_side confirmed（转移行天然唯一）
        for r in _signal_rows(conn, sym, "right_side", as_of):
            if r["state"] == "confirmed" and not _decided(
                    conn, sym, r["observed_on"], "entry", "right_side"):
                add(sym, r["observed_on"], "entry", "right_side",
                    r["state"], r["triggered"], r["details_json"])
        # ---- entry: falsification_breach 确认日（triggered=1；breached_today 非事件）
        for r in _signal_rows(conn, sym, "falsification_breach", as_of):
            if r["triggered"] == 1 and not _decided(
                    conn, sym, r["observed_on"], "entry", "falsification_breach"):
                add(sym, r["observed_on"], "entry", "falsification_breach",
                    r["state"], r["triggered"], r["details_json"])
        # ---- entry: box_entry（→buy_zone 转变 + 冷却期去抖）
        # 冷却基准 = 已录入决策 ∪ 本次扫描已产出候选（无状态纯读下的滚动冷却）
        last_point = conn.execute(
            "SELECT MAX(decision_date) FROM paper_decisions WHERE symbol=? "
            "AND signal_source='box_entry'", (sym,)).fetchone()[0]
        prev_state = None
        for r in _signal_rows(conn, sym, "box_position", as_of):
            if r["state"] == "buy_zone" and prev_state != "buy_zone":
                candidate = r["observed_on"]
                gap_ok = (last_point is None
                          or trading_days_between(conn, last_point,
                                                  candidate) > debounce)
                if gap_ok:
                    decided = _decided(conn, sym, candidate, "entry",
                                       "box_entry")
                    if not decided:
                        add(sym, candidate, "entry", "box_entry",
                            r["state"], r["triggered"], r["details_json"])
                    last_point = candidate  # 已决点同样占冷却
            prev_state = r["state"] if r["state"] is not None else prev_state
        # ---- exit（仅该股有 open position 时）
        if sym in open_syms:
            poss = conn.execute(
                "SELECT id, entry_date, deep_exit_line FROM paper_positions "
                "WHERE symbol=? AND status='open'", (sym,)).fetchall()
            for pos in poss:
                # 证伪确认日（entry 之后）
                for r in _signal_rows(conn, sym, "falsification_breach", as_of):
                    if (r["triggered"] == 1 and r["observed_on"] > pos["entry_date"]
                            and not _decided(conn, sym, r["observed_on"], "exit",
                                             "falsification_breach")):
                        add(sym, r["observed_on"], "exit", "falsification_breach",
                            r["state"], r["triggered"], r["details_json"])
                # 右侧 stopped_out
                for r in _signal_rows(conn, sym, "right_side", as_of):
                    if (r["state"] == "stopped_out"
                            and r["observed_on"] > pos["entry_date"]
                            and not _decided(conn, sym, r["observed_on"], "exit",
                                             "stopped_out")):
                        add(sym, r["observed_on"], "exit", "stopped_out",
                            r["state"], r["triggered"], r["details_json"])
                # deep_exit 首次破线日（bar 驱动）
                for bar in conn.execute(
                        "SELECT trade_date, close_raw FROM daily_bars "
                        "WHERE symbol=? AND trade_date>? ORDER BY trade_date",
                        (sym, pos["entry_date"])):
                    if float(bar["close_raw"]) < float(pos["deep_exit_line"]):
                        if not _decided(conn, sym, bar["trade_date"], "exit",
                                        "deep_exit"):
                            add(sym, bar["trade_date"], "exit", "deep_exit",
                                None, None,
                                json.dumps({"deep_exit_line": pos["deep_exit_line"],
                                            "close_raw": bar["close_raw"]}))
                        break  # 仅首个破线日

    points.sort(key=lambda p: (p["decision_date"], p["symbol"],
                               p["decision_type"], p["signal_source"]))
    for i, p in enumerate(points, 1):
        p["pick_id"] = i
    return points
