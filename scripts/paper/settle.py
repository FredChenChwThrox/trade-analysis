"""模拟盘结算：exit 决策落账 / timeout 兜底（停牌顺延）/ manual / reversal。

结算口径（设计 §3.2/§3.3）：ret = (exit_close×f_exit)/(entry_close×f_entry) − 1，
两因子取**结算时点** daily_bars 库内值（同版本 origin，因子重建会重算 open 仓位
收益——已知偏差声明）；pnl = notional × ret；hold_days 按交易日历（含停牌）。
"""
from __future__ import annotations

import argparse
import sqlite3
import json

from scripts.pipeline.db import DEFAULT_DB_PATH, connect
from scripts.paper import common as pc


def _settle_position(conn: sqlite3.Connection, pos: sqlite3.Row,
                     exit_date: str, exit_close: str, exit_source: str,
                     exit_decision_id: int | None) -> None:
    f_entry = conn.execute(
        "SELECT price_adj_factor FROM daily_bars WHERE symbol=? AND trade_date=?",
        (pos["symbol"], pos["entry_date"])).fetchone()
    f_exit = conn.execute(
        "SELECT price_adj_factor FROM daily_bars WHERE symbol=? AND trade_date=?",
        (pos["symbol"], exit_date)).fetchone()
    fe = (f_entry["price_adj_factor"] if f_entry else None) or 1.0
    fx = (f_exit["price_adj_factor"] if f_exit else None) or 1.0
    ret = (float(exit_close) * fx) / (float(pos["entry_close"]) * fe) - 1.0
    hold = pc.trading_days_between(conn, pos["entry_date"], exit_date)
    now = pc.utc_now() if hasattr(pc, "utc_now") else None
    from scripts.pipeline.db import utc_now
    conn.execute(
        """
        UPDATE paper_positions SET status='closed', exit_date=?, exit_close=?,
            exit_source=?, exit_decision_id=?, hold_days=?, ret=?, pnl=?,
            closed_at=?
        WHERE id=?
        """,
        (exit_date, exit_close, exit_source, exit_decision_id, hold, ret,
         float(pos["notional"]) * ret, utc_now(), pos["id"]))


def settle_exit_follow(conn: sqlite3.Connection, symbol: str,
                       exit_decision_id: int) -> None:
    """decide 录入 exit-follow 后立即落账（同事务，decide 调用）。"""
    dec = conn.execute(
        "SELECT * FROM paper_decisions WHERE id=?", (exit_decision_id,)).fetchone()
    if dec is None:
        return
    pos = conn.execute(
        "SELECT * FROM paper_positions WHERE symbol=? AND status='open' "
        "AND entry_date<=? ORDER BY entry_date LIMIT 1",
        (symbol, dec["decision_date"])).fetchone()
    if pos is None:
        return  # 无 open 仓（已平/无仓）——决策行保留，结算忽略
    if dec["close_used"] is None:
        return  # 防御：无价不结（§2.5）
    _settle_position(conn, pos, dec["decision_date"], dec["close_used"],
                     dec["signal_source"], dec["id"])


def run_settle(conn: sqlite3.Connection, cfg: dict, now_date: str) -> list[dict]:
    """timeout 兜底扫描：持有满 max_hold_days → 顺延至下一有 bar 日强制结算。"""
    max_hold = int(cfg["max_hold_days"])
    settled: list[dict] = []
    poss = conn.execute(
        "SELECT * FROM paper_positions WHERE status='open'").fetchall()
    for pos in poss:
        if pc.trading_days_between(conn, pos["entry_date"], now_date) < max_hold:
            continue
        days = pc._cal_rows(conn)
        import bisect
        i = bisect.bisect_left(days, pos["entry_date"])
        target_idx = i + max_hold - 1
        if target_idx >= len(days):
            continue
        target = days[target_idx]
        # 目标日有 bar → 当日结算；停牌无 bar → 顺延至下一有 bar 日
        bar = pc.get_bar(conn, pos["symbol"], target)
        if bar is not None and bar["close_raw"] is not None:
            actual = target
        else:
            actual = pc.next_bar_date(conn, pos["symbol"], target) or ""
            bar = pc.get_bar(conn, pos["symbol"], actual) if actual else None
        if actual is None or actual == "" or actual > now_date or bar is None \
                or bar["close_raw"] is None:
            continue  # 停牌顺延中 / 未到期
        _settle_position(conn, pos, actual, str(bar["close_raw"]), "timeout",
                         None)
        settled.append({"symbol": pos["symbol"],
                        "position_id": pos["id"], "exit_date": actual,
                        "exit_source": "timeout"})
    return settled


def manual_close(conn: sqlite3.Connection, symbol: str, reason: str,
                 now_date: str, cfg: dict, now_dt: str) -> int:
    """人工平仓：出 manual 决策行 + 按最新 bar 收盘结算（单列统计）。"""
    pos = conn.execute(
        "SELECT * FROM paper_positions WHERE symbol=? AND status='open' "
        "ORDER BY entry_date LIMIT 1", (symbol,)).fetchone()
    if pos is None:
        raise ValueError(f"{symbol} 无 open 仓位")
    bar = conn.execute(
        "SELECT trade_date, close_raw FROM daily_bars WHERE symbol=? "
        "ORDER BY trade_date DESC LIMIT 1", (symbol,)).fetchone()
    if bar is None:
        raise ValueError(f"{symbol} 无可用收盘价")
    conn.execute(
        """
        INSERT INTO paper_decisions (symbol, decision_date, decision_type,
            signal_source, signal_snapshot_json, decision, close_used,
            notional, late, note, run_id, created_at)
        VALUES (?, ?, 'exit', 'manual', ?, 'follow', ?, ?, 0, ?, ?, ?)
        """,
        (symbol, bar["trade_date"],
         json.dumps({"reason": reason, "manual": True}, ensure_ascii=False),
         str(bar["close_raw"]), float(cfg["notional_per_trade"]), reason,
         f"paper_{now_dt}", now_dt))
    did = conn.execute("SELECT MAX(id) FROM paper_decisions").fetchone()[0]
    _settle_position(conn, pos, bar["trade_date"], str(bar["close_raw"]),
                     "manual", did)
    return did


def reversal(conn: sqlite3.Connection, decision_id: int, reason: str,
             now_date: str, cfg: dict, now_dt: str) -> int:
    """冲正：原决策标 reversed_by（永久可见）；entry-follow 的 open 仓强制平仓。

    已结算（closed）仓位的 exit 决策不可冲正（v1 限制，如实拒绝）。
    """
    orig = conn.execute("SELECT * FROM paper_decisions WHERE id=?",
                        (decision_id,)).fetchone()
    if orig is None:
        raise ValueError(f"decision_id={decision_id} 不存在")
    if orig["reversed_by"] is not None:
        raise ValueError("该决策已被冲正")
    conn.execute(
        """
        INSERT INTO paper_decisions (symbol, decision_date, decision_type,
            signal_source, signal_snapshot_json, decision, close_used,
            notional, late, note, run_id, created_at)
        VALUES (?, ?, 'reversal', ?, ?, 'reversal', NULL, ?, 0, ?, ?, ?)
        """,
        (orig["symbol"], now_date, orig["signal_source"],
         json.dumps({"reverses": decision_id, "reason": reason},
                    ensure_ascii=False), float(cfg["notional_per_trade"]),
         reason, f"paper_{now_dt}", now_dt))
    rid = conn.execute("SELECT MAX(id) FROM paper_decisions").fetchone()[0]
    conn.execute("UPDATE paper_decisions SET reversed_by=? WHERE id=?",
                 (rid, decision_id))
    if orig["decision_type"] == "entry" and orig["decision"] == "follow":
        pos = conn.execute(
            "SELECT * FROM paper_positions WHERE entry_decision_id=? "
            "AND status='open'", (decision_id,)).fetchone()
        if pos is not None:
            bar = conn.execute(
                "SELECT trade_date, close_raw FROM daily_bars WHERE symbol=? "
                "ORDER BY trade_date DESC LIMIT 1", (orig["symbol"],)).fetchone()
            if bar is not None:
                _settle_position(conn, pos, bar["trade_date"],
                                 str(bar["close_raw"]), "reversal", rid)
    return rid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.paper.settle")
    sub = parser.add_subparsers(dest="cmd")
    p_scan = sub.add_parser("run", help="timeout 兜底扫描")
    p_scan.add_argument("--as-of", default=None)
    p_manual = sub.add_parser("manual-close", help="人工平仓")
    p_manual.add_argument("--symbol", required=True)
    p_manual.add_argument("--reason", required=True)
    p_rev = sub.add_parser("reversal", help="冲正决策")
    p_rev.add_argument("--id", type=int, required=True)
    p_rev.add_argument("--reason", required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    from scripts.paper.decide import _now_bj
    now_dt = _now_bj()
    now_date = now_dt[:10]
    conn = connect(args.db)
    try:
        cfg = pc.load_config()
        with conn:
            if args.cmd == "manual-close":
                did = manual_close(conn, args.symbol, args.reason, now_date,
                                   cfg, now_dt)
                print(f"OK manual_close decision_id={did}")
            elif args.cmd == "reversal":
                rid = reversal(conn, args.id, args.reason, now_date, cfg,
                               now_dt)
                print(f"OK reversal_id={rid}")
            else:
                settled = run_settle(conn, cfg, args.as_of or now_date)
                print(f"== settle: timeout 结算 {len(settled)} 笔")
                for s in settled:
                    print(f"   {s}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
