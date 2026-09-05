"""模拟盘统计：follow/skip/counter 三组 + 机械基线 + 判断力差值。

口径（设计 §5）：
- 主观组合 = follow 仓位（closed 用已结算 ret；open 用最新 bar 逐日盯市）；
- 机械基线 = 同决策点全集"信号即全跟"（退出也全跟：首个基线退出事件或 MTM），
  完全机械化、与主观 skip 无关（评审四.5）；
- 判断力差值 = 主观组合累计 pnl − 基线累计 pnl；
- skip 组分层：自主 skip vs 结构性 skip（constraint_tag='single_position'）；
- late 默认含并标注，--exclude-late 恒备对照；
- 样本 < 30 只输出描述统计（system_design §2.5：样本不足不下结论）。
"""
from __future__ import annotations

import sqlite3

from scripts.paper import common as pc


def _latest_bar(conn: sqlite3.Connection, symbol: str):
    return conn.execute(
        "SELECT trade_date, close_raw, price_adj_factor FROM daily_bars "
        "WHERE symbol=? ORDER BY trade_date DESC LIMIT 1", (symbol,)).fetchone()


def _factors(conn: sqlite3.Connection, symbol: str, d1: str, d2: str):
    f1 = conn.execute(
        "SELECT price_adj_factor FROM daily_bars WHERE symbol=? AND trade_date=?",
        (symbol, d1)).fetchone()
    f2 = conn.execute(
        "SELECT price_adj_factor FROM daily_bars WHERE symbol=? AND trade_date=?",
        (symbol, d2)).fetchone()
    return ((f1[0] if f1 else None) or 1.0, (f2[0] if f2 else None) or 1.0)


def _baseline_exit(conn: sqlite3.Connection, symbol: str, entry_date: str,
                   entry_close: float, deep_pct: float, as_of: str):
    """基线退出日：entry 后首个 falsification 确认 / stopped_out / 破 deep 线；
    无 → as_of 前最新 bar（MTM）。返回 (exit_date, exit_close)。"""
    deep_line = entry_close * (1.0 - float(deep_pct))
    bars = conn.execute(
        "SELECT trade_date, close_raw, price_adj_factor FROM daily_bars "
        "WHERE symbol=? AND trade_date>? ORDER BY trade_date",
        (symbol, entry_date)).fetchall()
    fb_days = {r[0] for r in conn.execute(
        "SELECT observed_on FROM signal_facts WHERE symbol=? "
        "AND signal='falsification_breach' AND triggered=1 AND observed_on>?",
        (symbol, entry_date))}
    so_days = {r[0] for r in conn.execute(
        "SELECT observed_on FROM signal_facts WHERE symbol=? "
        "AND signal='right_side' AND state='stopped_out' AND observed_on>?",
        (symbol, entry_date))}
    for b in bars:
        td, close = b["trade_date"], float(b["close_raw"])
        if td in fb_days or td in so_days or close < deep_line:
            return td, close
    if bars:
        return bars[-1]["trade_date"], float(bars[-1]["close_raw"])
    return entry_date, entry_close


def _entry_events(conn: sqlite3.Connection, as_of: str) -> list[dict]:
    """全历史 entry 事件集（基线口径：不剔已决、不剔单仓约束）。

    枚举规则同 common.enumerate_decision_points 的 entry 部分（tier 转变/
    right_side confirmed/falsification 确认日/box 转变+冷却）。
    """
    import bisect
    debounce = 5
    events: list[dict] = []
    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM watchlist WHERE active=1 ORDER BY symbol")]
    for sym in symbols:
        prev = None
        for r in pc._signal_rows(conn, sym, "tier_triggered", as_of):
            if r["state"] == "triggered" and prev != "triggered":
                events.append({"symbol": sym, "date": r["observed_on"],
                               "source": "tier_triggered"})
            prev = r["state"] if r["state"] is not None else prev
        for r in pc._signal_rows(conn, sym, "right_side", as_of):
            if r["state"] == "confirmed":
                events.append({"symbol": sym, "date": r["observed_on"],
                               "source": "right_side"})
        for r in pc._signal_rows(conn, sym, "falsification_breach", as_of):
            if r["triggered"] == 1:
                events.append({"symbol": sym, "date": r["observed_on"],
                               "source": "falsification_breach"})
        last_point = conn.execute(
            "SELECT MAX(decision_date) FROM paper_decisions WHERE symbol=? "
            "AND signal_source='box_entry'", (sym,)).fetchone()[0]
        prev = None
        for r in pc._signal_rows(conn, sym, "box_position", as_of):
            if r["state"] == "buy_zone" and prev != "buy_zone":
                gap_ok = (last_point is None
                          or pc.trading_days_between(conn, last_point,
                                                     r["observed_on"]) > debounce)
                if gap_ok:
                    events.append({"symbol": sym, "date": r["observed_on"],
                                   "source": "box_entry"})
                    last_point = r["observed_on"]
            prev = r["state"] if r["state"] is not None else prev
    events.sort(key=lambda e: (e["date"], e["symbol"]))
    return events


def compute_stats(conn: sqlite3.Connection, cfg: dict, *,
                  exclude_late: bool = False, symbol: str | None = None,
                  as_of: str | None = None) -> dict:
    deep_pct = float(cfg["deep_exit_pct"])
    notional = float(cfg["notional_per_trade"])
    sym_f = " AND symbol=?" if symbol else ""
    sym_args = (symbol,) if symbol else ()

    # ---- follow 组（positions；open 逐日盯市）
    poss = conn.execute(
        f"SELECT * FROM paper_positions WHERE 1=1{sym_f}", sym_args).fetchall()
    follow_n = wins = open_n = 0
    cum_pnl = 0.0
    rets: list[float] = []
    for pos in poss:
        if pos["status"] == "closed":
            ret = pos["ret"] or 0.0
        else:
            bar = _latest_bar(conn, pos["symbol"])
            if bar is None:
                continue
            fe, fx = _factors(conn, pos["symbol"], pos["entry_date"],
                              bar["trade_date"])
            ret = (float(bar["close_raw"]) * fx) / (
                float(pos["entry_close"]) * fe) - 1.0
            open_n += 1
        wins += 1 if ret > 0 else 0
        rets.append(ret)
        cum_pnl += float(pos["notional"]) * ret
    follow_n = len(rets)
    stats = {
        "follow": {"n": follow_n, "wins": wins,
                   "winrate": (wins / follow_n) if follow_n else None,
                   "avg_ret": (sum(rets) / follow_n) if follow_n else None,
                   "cum_pnl": cum_pnl, "open_n": open_n},
    }

    # ---- skip / counter（决策行；虚拟跟入收益）
    def _virtual(dec_row) -> float | None:
        bar = get_entry_bar = conn.execute(
            "SELECT price_adj_factor FROM daily_bars WHERE symbol=? "
            "AND trade_date=?",
            (dec_row["symbol"], dec_row["decision_date"])).fetchone()
        if bar is None or bar[0] is None:
            return None
        fe = bar[0] or 1.0
        entry_close = float(dec_row["close_used"] or 0)
        if entry_close <= 0:
            return None
        xd, xc = _baseline_exit(conn, dec_row["symbol"], dec_row["decision_date"],
                                entry_close, deep_pct,
                                as_of or dec_row["decision_date"])
        fx = _factors(conn, dec_row["symbol"], dec_row["decision_date"],
                      xd)[1]
        return (xc * fx) / (entry_close * fe) - 1.0

    for group, decision_val in (("skip", "skip"), ("counter", "counter")):
        rows = conn.execute(
            f"""SELECT * FROM paper_decisions WHERE decision_type='entry'
                AND decision=? AND reversed_by IS NULL{sym_f}""",
            (decision_val, *sym_args)).fetchall()
        if exclude_late:
            rows = [r for r in rows if not r["late"]]
        n = len(rows)
        vrets: list[float] = []
        structural = 0
        for r in rows:
            v = _virtual(r)
            if v is None:
                n -= 1
                continue
            vrets.append(v)
            if r["constraint_tag"]:
                structural += 1
        cum = sum(vrets) * notional
        if group == "skip":
            stats["skip"] = {
                "n": n, "structural_n": structural,
                "autonomous_n": n - structural,
                "missed_profit_pnl": sum(v for v in vrets if v > 0) * notional,
                "avoided_loss_pnl": sum(-v for v in vrets if v <= 0) * notional,
                "cum_virtual_pnl": cum,
            }
        else:
            correct = sum(1 for v in vrets if v < 0)
            stats["counter"] = {
                "n": n, "correct_n": correct,
                "correct_rate": (correct / n) if n else None,
                "cum_virtual_pnl": cum,
            }

    # ---- 机械基线（全点全跟）
    events = [e for e in _entry_events(conn, as_of or "9999-12-31")
              if symbol is None or e["symbol"] == symbol]
    base_rets: list[float] = []
    for e in events:
        bar = conn.execute(
            "SELECT close_raw, price_adj_factor FROM daily_bars "
            "WHERE symbol=? AND trade_date=?", (e["symbol"], e["date"])).fetchone()
        if bar is None or bar["close_raw"] is None:
            continue
        ec = float(bar["close_raw"])
        fe = bar["price_adj_factor"] or 1.0
        xd, xc = _baseline_exit(conn, e["symbol"], e["date"], ec, deep_pct,
                                as_of or "9999-12-31")
        fx = _factors(conn, e["symbol"], e["date"], xd)[1]
        base_rets.append((xc * fx) / (ec * fe) - 1.0)
    baseline_pnl = sum(base_rets) * notional
    stats["baseline"] = {"n": len(base_rets), "cum_pnl": baseline_pnl,
                         "winrate": (sum(1 for r in base_rets if r > 0)
                                     / len(base_rets)) if base_rets else None}

    # ---- 判断力差值 + late + 样本
    stats["judgement_diff"] = cum_pnl - baseline_pnl
    late_q = conn.execute(
        f"SELECT COUNT(*) FROM paper_decisions WHERE late=1{sym_f}",
        sym_args).fetchone()[0]
    stats["late_count"] = late_q
    total_decisions = conn.execute(
        f"SELECT COUNT(*) FROM paper_decisions WHERE reversed_by IS NULL{sym_f}",
        sym_args).fetchone()[0]
    stats["sample_warning"] = (follow_n + stats["skip"]["n"]
                               + stats["counter"].get("n", 0)) < 30
    return stats


def format_stats(s: dict) -> str:
    lines = ["== 模拟盘统计 =="]
    f = s["follow"]
    wr = f"{f['winrate']:.0%}" if f["winrate"] is not None else "—"
    ar = f"{f['avg_ret']:+.2%}" if f["avg_ret"] is not None else "—"
    lines.append(f"follow: {f['n']} 笔（open {f['open_n']}）胜率 {wr} "
                 f"平均 {ar} 累计 pnl {f['cum_pnl']:+,.0f}")
    sk = s["skip"]
    lines.append(f"skip: {sk['n']} 笔（自主 {sk['autonomous_n']} / 结构性 "
                 f"{sk['structural_n']}）错过盈利 {sk['missed_profit_pnl']:+,.0f} "
                 f"躲过亏损 {sk['avoided_loss_pnl']:+,.0f}")
    c = s["counter"]
    cr = f"{c['correct_rate']:.0%}" if c["correct_rate"] is not None else "—"
    lines.append(f"counter: {c['n']} 笔 看反正确率 {cr}")
    b = s["baseline"]
    bwr = f"{b['winrate']:.0%}" if b["winrate"] is not None else "—"
    lines.append(f"机械基线（全跟）: {b['n']} 点 胜率 {bwr} "
                 f"累计 pnl {b['cum_pnl']:+,.0f}")
    lines.append(f"判断力差值（主观−基线）: {s['judgement_diff']:+,.0f}")
    lines.append(f"late: {s['late_count']} 条（统计默认含 late；--exclude-late 对照）")
    if s["sample_warning"]:
        lines.append("⚠️ 样本不足 30 笔：以上为描述统计，不下判断力结论（§2.5）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from scripts.pipeline.db import DEFAULT_DB_PATH, connect
    parser = argparse.ArgumentParser(prog="scripts.paper.stats")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--exclude-late", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        cfg = pc.load_config()
        s = compute_stats(conn, cfg, exclude_late=args.exclude_late,
                          symbol=args.symbol)
        print(format_stats(s))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
