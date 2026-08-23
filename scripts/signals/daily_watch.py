"""日频监测（D2.2，设计 §5.4、§5.1、§2.5）。

对每个交易日（只用当日及之前数据，无未来函数）相对当前生效排期卡计算：

1. 档位 tier_proximity / tier_triggered：
   - 临近：现价（close_raw，不复权）距任一价区最近边界 ≤ tier_proximity_pct
     （默认 3%，分母为边界价，恰好 3% 算临近——边界语义锁定为 ≤）；
     现价已在价区内不计临近（那是触发）。
   - 触发：收盘价进入价区；第二、三档还要求同一 anchor_id 下当前完成周活跃
     衰竭信号 ≥ min_active_signals（默认 2，exhaustion.count_active_signals，
     完成周取 ≤ 当日的最近 week_end_date）。
2. 证伪线 falsification_breach：收盘 ≤ 证伪线 ×(1−breach_pct)（默认 1%，
   恰好 1% 算跌破——边界语义锁定为 ≤）连续 confirm_days（默认 2）个交易日
   → 有效跌破，确认日 triggered=1；之后仍低于线保持 active（holding），
   收回线上转 inactive（recovered）。跨卡片版本连续计数重置（线不同）。
3. 波段箱体 box_position：只对卡片存档边界分类（above_box / sell_zone /
   mid_box / buy_zone / below_box / box_breached），不重新识别箱体（§5.4）。
4. 均线口径纪律 ma_comparison：indicators_daily 的 MA 为复权口径，与不复权
   现价比较前必须 ÷ 当日 price_adj_factor 折回（§5.4、§9.5 硬门槛），
   details 同时记录复权原值、因子与折回值。

口径：卡片价区/证伪线/箱体/现价全走不复权，禁止跨尺度直接比。

无 active 卡片：卡片相关信号一律不产出，写 daily_watch 行 state=incomplete
（reason=no_active_card），绝不猜（§2.5）。公司行为冻结期间（
corporate_action.unresolved_suspensions 非空，除权日起）卡片触发挂起，
写 daily_watch 行 state=suspended，不输出伪触发（§5.4b 第一步）。

派生表重算语义：DELETE 本模块管理的 signal 行后全量重插（§2.2 第 3 类），
卡片信号只对各版本实际生效区间计算（§5.1）。

CLI：
    uv run python -m scripts.signals.daily_watch <symbol> [--as-of D] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals import cards as card_mod
from scripts.signals import corporate_action as ca_mod
from scripts.signals.common import RULE_VERSION, WEEKLY_SIGNALS, load_params
from scripts.signals.exhaustion import count_active_signals

DAILY_WATCH_SIGNALS = [
    "daily_watch", "tier_proximity", "tier_triggered",
    "falsification_breach", "box_position", "ma_comparison",
]

MA_WINDOWS = ("ma20", "ma60")


@dataclass
class DailyWatchResult:
    symbol: str = ""
    run_id: str = ""
    as_of: str = ""
    config_hash: str = ""
    rule_version: str = RULE_VERSION
    status: str = "ok"            # ok / incomplete
    reason: str = ""
    days: int = 0
    facts_written: int = 0
    latest: dict = field(default_factory=dict)   # as_of 当日各信号明细
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"{self.symbol} run_id={self.run_id} rule_version={self.rule_version} "
            f"status={self.status}" + (f"（{self.reason}）" if self.reason else ""),
            f"as_of={self.as_of} 监测日 {self.days} 天，signal_facts {self.facts_written} 行",
        ]
        cur = self.latest
        if cur:
            lines.append(f"当日 {cur['trade_date']} 收盘(不复权)={cur['close_raw']}")
            if cur.get("card"):
                c = cur["card"]
                lines.append(
                    f"  卡片 {c['card_version_id']}（生效 {c['effective_from']} 起）")
            if cur.get("suspended"):
                lines.append(f"  ⚠ 公司行为冻结中: {cur['suspended']}")
            for name in ("tier_proximity", "tier_triggered", "falsification_breach",
                         "box_position", "ma_comparison"):
                s = cur.get(name)
                if s:
                    lines.append(f"  [{name}] state={s['state']} "
                                 f"triggered={s['triggered']} reason={s.get('reason')}")
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------- 数据加载

def _load_bars(conn: sqlite3.Connection, symbol: str, as_of: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT trade_date, close_raw, low_raw, volume_raw, price_adj_factor,
               share_factor
        FROM daily_bars WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date
        """,
        (symbol, as_of),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_ma(conn: sqlite3.Connection, symbol: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT trade_date, ma20, ma60 FROM indicators_daily WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    return {r["trade_date"]: {"ma20": r["ma20"], "ma60": r["ma60"]} for r in rows}


def _latest_completed_week(conn: sqlite3.Connection, symbol: str,
                           trade_date: str) -> str | None:
    """≤ 当日的最近完成周（weekly_bars 只存完成周）；无周线表数据时回退到
    signal_facts 中周信号的最大观测日。"""
    r = conn.execute(
        "SELECT MAX(week_end_date) AS d FROM weekly_bars "
        "WHERE symbol = ? AND week_end_date <= ?",
        (symbol, trade_date),
    ).fetchone()
    if r and r["d"]:
        return r["d"]
    r = conn.execute(
        f"SELECT MAX(observed_on) AS d FROM signal_facts WHERE symbol = ? "
        f"AND observed_on <= ? AND signal IN "
        f"({', '.join('?' * len(WEEKLY_SIGNALS))})",
        (symbol, trade_date, *WEEKLY_SIGNALS),
    ).fetchone()
    return r["d"] if r else None


# ---------------------------------------------------------------- 单日计算（纯判定）

def tier_states(close: Decimal, tiers: list[dict], proximity_pct: float,
                signals_met: bool, min_active: int) -> tuple[dict, dict]:
    """档位临近与触发判定。返回 (proximity_detail, triggered_detail)。

    - 临近：|close − 最近边界| / 边界 ≤ proximity_pct（价区内不计）；
    - 触发：收盘进区；tier ≥ 2 需 signals_met（同锚点活跃衰竭信号 ≥ min_active）。
    """
    prox = {"close_raw": str(close), "threshold_pct": proximity_pct, "tiers": []}
    trig = {"close_raw": str(close), "min_active_signals": min_active, "tiers": []}
    prox_hit = None
    trig_hit = None
    pending = None
    for t in sorted(tiers, key=lambda x: x.get("tier") or 0):
        lo, hi = t.get("zone_low"), t.get("zone_high")
        if lo is None or hi is None:
            continue
        in_zone = lo <= close <= hi
        entry = {"tier": t["tier"], "zone_low": str(lo), "zone_high": str(hi),
                 "in_zone": in_zone}
        if in_zone:
            entry["distance_to_nearest_boundary_pct"] = 0.0
            need_signals = (t.get("tier") or 1) >= 2
            entry["requires_signals"] = need_signals
            entry["signals_met"] = (signals_met if need_signals else None)
            trig["tiers"].append(entry)
            if need_signals and not signals_met:
                pending = t["tier"]
            else:
                trig_hit = t["tier"]
        else:
            boundary = lo if close < lo else hi
            dist = abs(close - boundary) / boundary
            entry["nearest_boundary"] = str(boundary)
            entry["distance_to_nearest_boundary_pct"] = float(dist)
            entry["within_proximity"] = dist <= Decimal(str(proximity_pct))
            prox["tiers"].append(entry)
            if entry["within_proximity"] and prox_hit is None:
                prox_hit = t["tier"]
    prox["state"] = "active" if prox_hit is not None else "inactive"
    prox["reason"] = (f"within_{proximity_pct:.0%}_of_tier_{prox_hit}"
                      if prox_hit is not None else "no_tier_within_threshold")
    if trig_hit is not None:
        trig["state"], trig["triggered"] = "triggered", 1
        trig["reason"] = f"close_in_tier_{trig_hit}_zone"
    elif pending is not None:
        trig["state"], trig["triggered"] = "pending_signals", 0
        trig["reason"] = (f"tier_{pending}_zone_entered_but_active_signals"
                          f"<{min_active}")
    else:
        trig["state"], trig["triggered"] = "inactive", 0
        trig["reason"] = "no_tier_zone_entered"
    return prox, trig


def falsification_update(close: Decimal, line: Decimal, breach_pct: float,
                         confirm_days: int, breach_run: int) -> tuple[dict, int]:
    """证伪线单日判定。breach_run = 截至昨日的连续跌破日数；返回 (detail, 新 run)。"""
    threshold = line * (Decimal("1") - Decimal(str(breach_pct)))
    breached = close <= threshold  # 恰好 breach_pct 算跌破（≤ 锁定）
    run = breach_run + 1 if breached else 0
    det = {
        "close_raw": str(close), "invalidation_line": str(line),
        "breach_pct": breach_pct, "breach_threshold": str(threshold),
        "breached_today": breached, "consecutive_breach_days": run,
        "confirm_days": confirm_days,
    }
    confirmed = run >= confirm_days
    if confirmed:
        det["state"] = "active"
        det["triggered"] = 1 if run == confirm_days else 0
        det["reason"] = "confirmed" if run == confirm_days else "holding"
    elif breached:
        det["state"] = "watching"
        det["triggered"] = 0
        det["reason"] = f"breach_day_{run}_of_{confirm_days}"
    else:
        det["state"] = "inactive"
        det["triggered"] = 0
        det["reason"] = "recovered" if breach_run >= confirm_days else "no_breach"
    return det, run


def box_position_state(close: Decimal, box: dict) -> dict:
    """波段箱体位置分类（只监测存档边界，§5.4）。"""
    det = {"close_raw": str(close),
           "boundaries": {k: str(v) for k, v in sorted(box.items())}}
    inv = box.get("box_invalidation")
    lo, hi = box.get("box_low"), box.get("box_high")
    b_lo, b_hi = box.get("buy_zone_low"), box.get("buy_zone_high")
    s_lo, s_hi = box.get("sell_zone_low"), box.get("sell_zone_high")
    if inv is not None and close < inv:
        pos = "box_breached"
    elif hi is not None and close > hi:
        pos = "above_box"
    elif s_lo is not None and s_hi is not None and s_lo <= close <= s_hi:
        pos = "sell_zone"
    elif b_lo is not None and b_hi is not None and b_lo <= close <= b_hi:
        pos = "buy_zone"
    elif lo is not None and close < lo:
        pos = "below_box"
    else:
        pos = "mid_box"
    det["state"] = pos
    det["triggered"] = 1 if pos in ("buy_zone", "sell_zone", "box_breached") else 0
    det["reason"] = pos
    return det


def ma_comparison_state(close_raw: float, factor: float | None,
                        ma_row: dict | None) -> dict:
    """口径纪律：复权均线 ÷ 当日 price_adj_factor 折回后与不复权现价比（§5.4）。"""
    det: dict = {"close_raw": close_raw, "price_adj_factor": factor, "mas": {}}
    if not factor or factor <= 0:
        det.update(state="incomplete", triggered=0, reason="missing_adj_factor")
        return det
    compared = 0
    positions = []
    for name in MA_WINDOWS:
        ma_adj = (ma_row or {}).get(name)
        if ma_adj is None:
            det["mas"][name] = {"adjusted": None, "reason": "missing_indicator"}
            continue
        raw_equiv = ma_adj / factor
        pos = "above" if close_raw > raw_equiv else (
            "below" if close_raw < raw_equiv else "at")
        det["mas"][name] = {
            "adjusted": ma_adj, "raw_equiv": raw_equiv,
            "position": pos,
        }
        compared += 1
        positions.append(pos)
    if compared == 0:
        det.update(state="incomplete", triggered=0, reason="missing_indicators")
    else:
        state = "above" if all(p == "above" for p in positions) else (
            "below" if all(p == "below" for p in positions) else "mixed")
        det.update(state=state, triggered=0, reason="adjusted_ma_folded_by_factor")
    return det


# ---------------------------------------------------------------- 全量重算

def run_daily_watch(
    conn: sqlite3.Connection,
    symbol: str,
    as_of: str | None = None,
    *,
    run_id: str | None = None,
    params: dict | None = None,
    config_hash: str | None = None,
) -> DailyWatchResult:
    """重算该股日频监测信号（调用方负责事务/提交）。"""
    started_at = utc_now()
    if params is None or config_hash is None:
        params, config_hash = load_params()
    if as_of is None:
        r = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars WHERE symbol = ?", (symbol,)
        ).fetchone()
        as_of = r["d"] if r and r["d"] else None
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"daily_watch_{symbol}_{now_compact}"
    res = DailyWatchResult(symbol=symbol, run_id=run_id, as_of=as_of or "",
                           config_hash=config_hash)
    if as_of is None:
        res.status, res.reason = "incomplete", "no_daily_bars"
        return res

    dw = params["daily_watch"]
    breach_pct = float(dw["falsification"]["breach_pct"])
    confirm_days = int(dw["falsification"]["confirm_days"])
    proximity_pct = float(dw["tier_proximity_pct"])
    min_active = int(params["exhaustion"]["min_active_signals"])

    bars = _load_bars(conn, symbol, as_of)
    ma_map = _load_ma(conn, symbol)
    versions = card_mod.load_card_versions(conn, symbol)
    suspensions = ca_mod.unresolved_suspensions(conn, symbol)
    frozen_from = min((s["ex_date"] for s in suspensions), default=None)

    now = utc_now()
    conn.execute(
        f"DELETE FROM signal_facts WHERE symbol = ? AND signal IN "
        f"({', '.join('?' * len(DAILY_WATCH_SIGNALS))})",
        (symbol, *DAILY_WATCH_SIGNALS),
    )

    def write(day: str, signal: str, state: str, triggered: int,
              details: dict, card_id: str | None) -> None:
        details = dict(details)
        details["card_version_id"] = card_id
        conn.execute(
            """
            INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
                triggered, active_until, details_json, run_id, rule_version,
                config_hash, created_at)
            VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (symbol, day, signal, state, triggered,
             json.dumps(details, ensure_ascii=False, sort_keys=True),
             run_id, RULE_VERSION, config_hash, now),
        )
        res.facts_written += 1

    rows_for_latest: dict = {}
    breach_run = 0
    breach_card_id: str | None = None
    active_count_cache: dict[str, dict] = {}

    for bar in bars:
        day = bar["trade_date"]
        close = Decimal(str(bar["close_raw"]))
        card = card_mod.card_for_day(versions, day)
        if card is None:
            continue  # 无卡片生效日不产出卡片信号（§5.1）
        res.days += 1
        day_rows: dict = {"trade_date": day, "close_raw": bar["close_raw"],
                          "card": {"card_version_id": card.card_version_id,
                                   "effective_from": card.effective_from}}

        # ---- 公司行为冻结（§5.4b 第一步）：除权日起挂起卡片触发
        if frozen_from is not None and day >= frozen_from:
            det = {"reason": "suspended_corporate_action",
                   "suspensions": [{"ca_id": s["ca_id"], "ex_date": s["ex_date"],
                                    "action_type": s["action_type"]}
                                   for s in suspensions]}
            write(day, "daily_watch", "suspended", 0, det, card.card_version_id)
            day_rows["suspended"] = det["suspensions"]
            rows_for_latest = day_rows
            continue

        # ---- 档位
        if card.tiers:
            week_end = _latest_completed_week(conn, symbol, day)
            if week_end is None:
                counts = {"active_count": 0, "active_signals": [],
                          "anchor_id": None, "meets_min": False}
                sig_reason = "no_weekly_signals"
            else:
                if week_end not in active_count_cache:
                    active_count_cache[week_end] = count_active_signals(
                        conn, symbol, week_end, min_active)
                counts = active_count_cache[week_end]
                sig_reason = None
            signals_met = bool(counts["meets_min"])
            prox, trig = tier_states(close, card.tiers, proximity_pct,
                                     signals_met, min_active)
            sig_info = {"completed_week": week_end,
                        "active_count": counts["active_count"],
                        "active_signals": counts["active_signals"],
                        "anchor_id": counts["anchor_id"]}
            if sig_reason:
                sig_info["reason"] = sig_reason
            trig.update(sig_info)
            prox_state = prox.pop("state")
            trig_state, trig_flag = trig.pop("state"), trig.pop("triggered")
            write(day, "tier_proximity", prox_state,
                  1 if prox_state == "active" else 0, prox, card.card_version_id)
            write(day, "tier_triggered", trig_state, trig_flag, trig,
                  card.card_version_id)
            day_rows["tier_proximity"] = {"state": prox_state, "triggered":
                                          1 if prox_state == "active" else 0,
                                          "reason": prox["reason"]}
            day_rows["tier_triggered"] = {"state": trig_state, "triggered": trig_flag,
                                          "reason": trig["reason"]}
        else:
            write(day, "tier_triggered", "incomplete", 0,
                  {"reason": "missing_price_tiers"}, card.card_version_id)

        # ---- 证伪线（跨版本重置连续计数）
        if card.invalidation_line is None:
            write(day, "falsification_breach", "incomplete", 0,
                  {"reason": "missing_invalidation_line"}, card.card_version_id)
        else:
            if breach_card_id != card.card_version_id:
                breach_run, breach_card_id = 0, card.card_version_id
            det, breach_run = falsification_update(
                close, card.invalidation_line, breach_pct, confirm_days, breach_run)
            state, triggered = det.pop("state"), det.pop("triggered")
            write(day, "falsification_breach", state, triggered, det,
                  card.card_version_id)
            day_rows["falsification_breach"] = {"state": state, "triggered": triggered,
                                                "reason": det["reason"]}

        # ---- 波段箱体
        if card.swing_box:
            det = box_position_state(close, card.swing_box)
            state, triggered = det.pop("state"), det.pop("triggered")
            write(day, "box_position", state, triggered, det, card.card_version_id)
            day_rows["box_position"] = {"state": state, "triggered": triggered,
                                        "reason": det["reason"]}

        # ---- 均线口径纪律
        det = ma_comparison_state(bar["close_raw"], bar["price_adj_factor"],
                                  ma_map.get(day))
        state, triggered = det.pop("state"), det.pop("triggered")
        write(day, "ma_comparison", state, triggered, det, card.card_version_id)
        day_rows["ma_comparison"] = {"state": state, "triggered": triggered,
                                     "reason": det["reason"]}

        rows_for_latest = day_rows

    # ---- as_of 当日无生效卡 → incomplete（§2.5 不猜）
    active_card = card_mod.load_active_card(conn, symbol, as_of)
    if active_card is None:
        reason = "no_active_card" if not versions else "card_not_effective_at_as_of"
        write(as_of, "daily_watch", "incomplete", 0, {"reason": reason}, None)
        res.status, res.reason = "incomplete", reason
        rows_for_latest = {"trade_date": as_of, "close_raw": bars[-1]["close_raw"]}
    res.latest = rows_for_latest

    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, card_version_id,
            status, error, started_at, finished_at)
        VALUES (?, 'daily_watch', ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (run_id, utc_now(), as_of, config_hash, RULE_VERSION,
         active_card.card_version_id if active_card else None,
         "success" if res.status == "ok" else "degraded", started_at, utc_now()),
    )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.signals.daily_watch")
    parser.add_argument("symbol")
    parser.add_argument("--as-of", default=None, help="数据截止交易日，默认最新 bar")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        with conn:  # DELETE + 重插 + run 记录同一事务（§4.3）
            res = run_daily_watch(conn, args.symbol, as_of=args.as_of)
        print(res)
        return 0 if res.status == "ok" else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
