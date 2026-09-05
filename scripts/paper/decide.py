"""模拟盘决策录入：--pending 列待决点 / --pick N 录三态决策。

反作弊（设计 §2.3）：成交价系统取（close_used 来自决策日 bar，不可自填）；
窗口 T 盘后→T+1 收盘（超窗 late 标注）；append-only（录错走 reversal）；
一点一决（UNIQUE 冲突即拒绝）。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scripts.pipeline.db import DEFAULT_DB_PATH, connect
from scripts.paper import common as pc


def _deep_exit_line(pt: dict, cfg: dict) -> str:
    """深度脱离线：档位入场取触发档 zone_low×(1−pct)；其余 entry_close×(1−pct)。"""
    pct = float(cfg["deep_exit_pct"])
    close = float(pt["close_used"])
    zone_low = None
    if pt["signal_source"] == "tier_triggered":
        snap = json.loads(pt["signal_snapshot_json"])
        det = snap.get("details_json") or "{}"
        if isinstance(det, str):
            try:
                det = json.loads(det)
            except json.JSONDecodeError:
                det = {}
        for t in (det.get("tiers") or []) if isinstance(det, dict) else []:
            if t.get("zone_low") is not None:
                zl = float(t["zone_low"])
                # 触发档 = 收盘所在档（zone_low ≤ close 中最大者）
                if zl <= close and (zone_low is None or zl > zone_low):
                    zone_low = zl
    base = zone_low if zone_low is not None else close
    return f"{base * (1.0 - pct):.6f}"


def record_decision(conn: sqlite3.Connection, pt: dict, decision: str,
                    note: str, now_dt: str, cfg: dict) -> int:
    """录一行决策（调用方负责事务）。违反规则抛 ValueError。"""
    symbol, date = pt["symbol"], pt["decision_date"]
    dtype, source = pt["decision_type"], pt["signal_source"]
    now_date = now_dt[:10]
    if pc._decided(conn, symbol, date, dtype, source):
        raise ValueError("该决策点已录入（一点一决）")
    if decision not in ("follow", "skip", "counter"):
        raise ValueError(f"非法 decision: {decision}")
    if dtype == "exit" and decision == "counter":
        raise ValueError("exit 决策点仅允许 follow（平仓）/ skip（继续持有）")
    constraint = pt.get("constraint_tag")
    if constraint == "single_position" and decision in ("follow", "counter"):
        raise ValueError("一股一仓：该股已有 open 仓位，entry 决策点仅允许 skip")
    late = 1 if pc.is_late(conn, date, now_date,
                           int(cfg["decision_window_days"])) else 0
    close = pt["close_used"]
    quantity = None
    if dtype == "entry" and decision == "follow":
        notional = float(cfg["notional_per_trade"])
        lots = int(notional / float(close)) // 100
        if lots <= 0:
            raise ValueError(f"名义 {notional:.0f} 不足一手（close={close}），"
                             f"请加大 notional_per_trade")
        quantity = lots * 100
    conn.execute(
        """
        INSERT INTO paper_decisions (symbol, decision_date, decision_type,
            signal_source, signal_snapshot_json, decision, close_used, quantity,
            notional, constraint_tag, late, note, run_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, date, dtype, source, pt["signal_snapshot_json"], decision,
         close, quantity, float(cfg["notional_per_trade"]), constraint, late,
         note or None, f"paper_{now_dt}", now_dt))
    did = conn.execute("SELECT MAX(id) FROM paper_decisions").fetchone()[0]
    if dtype == "entry" and decision == "follow":
        conn.execute(
            """
            INSERT INTO paper_positions (symbol, entry_decision_id, entry_date,
                entry_close, quantity, notional, deep_exit_line, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (symbol, did, date, close, quantity,
             float(cfg["notional_per_trade"]), _deep_exit_line(pt, cfg)))
    if dtype == "exit" and decision == "follow":
        from scripts.paper import settle as ps
        ps.settle_exit_follow(conn, symbol, did)
    return did


def _now_bj() -> str:
    """北京时区 ISO 时间（CN 交易语境的"今天"以北京日期为准）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.paper.decide")
    parser.add_argument("--pending", action="store_true", help="列待决策点")
    parser.add_argument("--pick", type=int, default=None, help="待决点编号")
    parser.add_argument("--decision", choices=["follow", "skip", "counter"])
    parser.add_argument("--note", default="")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        cfg = pc.load_config()
        now_dt = _now_bj()
        as_of = args.as_of or now_dt[:10]
        points = pc.enumerate_decision_points(conn, as_of, cfg)
        if args.pending or args.pick is None:
            if not points:
                print("（无待决策点）")
            for p in points:
                tag = f" [{p['constraint_tag']}]" if p["constraint_tag"] else ""
                print(f"#{p['pick_id']:>3} {p['decision_date']} {p['symbol']:<11}"
                      f" {p['decision_type']:<5} {p['signal_source']:<22}"
                      f" close={p['close_used']}{tag}")
            return 0
        pt = next((p for p in points if p["pick_id"] == args.pick), None)
        if pt is None:
            print(f"ERROR: 编号 {args.pick} 不在待决清单（--pending 查看）")
            return 1
        if not args.decision:
            print("ERROR: 需要 --decision follow|skip|counter")
            return 1
        with conn:
            late_flag = pc.is_late(conn, pt["decision_date"], now_date,
                                   int(cfg["decision_window_days"]))
            did = record_decision(conn, pt, args.decision, args.note,
                                  now_dt, cfg)
        print(f"OK decision_id={did} {pt['symbol']} {pt['decision_date']} "
              f"{pt['decision_type']}/{pt['signal_source']} "
              f"→ {args.decision}" + (" [LATE]" if late_flag else ""))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
