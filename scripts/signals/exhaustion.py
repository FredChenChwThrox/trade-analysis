"""衰竭信号（D2.1，设计 §5.3）。

五项信号逐完成周按当时可见数据计算（无未来函数，§5.1）：

1. 恐慌型 panic：当前周调整后成交量 ≥ 此前 vol_ma_weeks（默认 20）个完成周均量
   × vol_multiple（默认 2.0），且满足形态之一——
   长下影（下影 ≥ 实体 2 倍 且 ≥ 全周振幅 35%）或大阳线（收 > 开、实体 ≥ 振幅
   60%、周涨幅 ≥ 5%）。确认周起活跃 active_weeks.panic（默认 4）个完成周。
2. 干涸型 dry_up：当前周成交量 ≤ 下跌起点后前 base_weeks（默认 4）个完成周
   （不含当前周，§4.1 shift(1) 纪律）均量 × vol_ratio（默认 0.50）；
   可用基数不足 4 周时不判定（state=inactive，原因码 insufficient_base_weeks）。
   只在条件持续满足时活跃。
3. 三周不创新低 no_new_low_3w：恐慌低点周之后已有 3 个完成周且这 3 周复权最低价
   均未低于锚点复权最低价 → 确认周触发；之后持续活跃直到某周最低价再次跌破
   锚点最低价（原因码 new_low）；确认前跌破则该锚点下永不触发
   （broken_before_confirm）。
4. 周线底背离 divergence：左右各 pivot_side_weeks（默认 2）个完成周确认 pivot
   low（严格低于窗口内其余 4 周）；pivot 在确认周（pivot 周 + side 周）才可参与，
   最近两个已确认 pivot 位于 lookback_weeks（默认 26）窗口内、后一个复权收盘价
   低于前一个，且 RSI(12) 或 MACD 柱高于前一个 → 信号记录在确认周，不回填
   （§5.3、§9.2）。确认周起活跃 active_weeks.divergence（默认 4）个完成周。
5. 持续时间 duration：当前周距下跌起点周的完成周数 ≥ duration_weeks（默认 8），
   在当前下跌 episode 内持续活跃。

episode 结束（§5.3）：同一恐慌锚点（anchor_id 身份）下，首个周线收盘高于下跌
起点收盘的完成周起，旧 episode 结束，该锚点全部信号转为 inactive
（原因码 episode_ended）。恐慌锚点身份变化（新恐慌周 / fallback 变化）即进入
新 episode，旧信号不再计入当前得分。

details_json 含参与判断的日期、原值、阈值、锚点与原因码（§5.1）。
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from scripts.signals.common import WEEKLY_SIGNALS, WeekBar

# ---------------------------------------------------------------- 单项条件（纯函数）

def candle_metrics(w: WeekBar) -> dict:
    """K 线形态度量：实体/下影/振幅（复权口径）。"""
    body = abs(w.close - w.open)
    lower_shadow = min(w.open, w.close) - w.low
    rng = w.high - w.low
    return {"body": body, "lower_shadow": lower_shadow, "range": rng}


def _long_lower_shadow(w: WeekBar, p: dict) -> bool:
    m = candle_metrics(w)
    if m["range"] <= 0:
        return False
    return (m["lower_shadow"] >= p["lower_shadow_body_ratio"] * m["body"]
            and m["lower_shadow"] >= p["lower_shadow_range_pct"] * m["range"])


def _big_yang(weeks: list[WeekBar], i: int, p: dict) -> bool:
    if i < 1:
        return False
    w = weeks[i]
    m = candle_metrics(w)
    if m["range"] <= 0 or w.close <= w.open:
        return False
    gain = w.close / weeks[i - 1].close - 1.0
    return (m["body"] >= p["big_yang_body_pct"] * m["range"]
            and gain >= p["big_yang_gain_pct"])


def panic_condition(weeks: list[WeekBar], i: int, params: dict) -> bool:
    """恐慌型条件（锚点识别与 panic 信号共用同一判定，§5.2/§5.3）。

    均量基数 = 此前 vol_ma_weeks 个完成周（不含当前周，§4.1 shift(1)）；
    样本不足返回 False（不拿短窗口冒充，§4.1）。
    """
    p = params["exhaustion"]["panic"]
    n = p["vol_ma_weeks"]
    if i < n:
        return False
    base = sum(w.volume for w in weeks[i - n:i]) / n
    if weeks[i].volume < p["vol_multiple"] * base:
        return False
    return _long_lower_shadow(weeks[i], p) or _big_yang(weeks, i, p)


def dryup_state(weeks: list[WeekBar], i: int, decline_idx: int,
                params: dict) -> tuple[bool | None, dict]:
    """干涸型。返回 (cond, details)；cond=None 表示基数不足 4 周不判定。"""
    d = params["exhaustion"]["dryup"]
    bw = d["base_weeks"]
    base_idx = [k for k in range(decline_idx + 1, decline_idx + 1 + bw) if k < i]
    det = {
        "decline_week": weeks[decline_idx].week_end_date,
        "base_weeks": [weeks[k].week_end_date for k in base_idx],
        "base_volumes": [weeks[k].volume for k in base_idx],
        "required_base_weeks": bw,
        "vol_ratio_threshold": d["vol_ratio"],
        "current_volume": weeks[i].volume,
    }
    if len(base_idx) < bw:
        det["reason"] = "insufficient_base_weeks"
        return None, det
    base_mean = sum(det["base_volumes"]) / bw
    det["base_mean"] = base_mean
    det["threshold_volume"] = d["vol_ratio"] * base_mean
    cond = weeks[i].volume <= det["threshold_volume"]
    det["reason"] = "condition_met" if cond else "condition_not_met"
    return cond, det


def nnl_state(weeks: list[WeekBar], i: int, anchor_idx: int, anchor_low: float,
              params: dict) -> tuple[str, bool, dict]:
    """三周不创新低。返回 (state, triggered, details)。"""
    need = params["exhaustion"]["no_new_low_weeks"]
    det = {
        "anchor_week": weeks[anchor_idx].week_end_date,
        "anchor_low": anchor_low,
        "required_weeks": need,
        "weeks_since_anchor": i - anchor_idx,
        "current_low": weeks[i].low,
    }
    if i - anchor_idx < need:
        det["reason"] = "waiting_confirm"
        return "inactive", False, det
    first = [weeks[k] for k in range(anchor_idx + 1, anchor_idx + 1 + need)]
    det["confirm_weeks"] = [w.week_end_date for w in first]
    det["confirm_lows"] = [w.low for w in first]
    if any(w.low < anchor_low for w in first):
        det["reason"] = "broken_before_confirm"
        return "inactive", False, det
    if weeks[i].low < anchor_low:
        det["reason"] = "new_low"
        return "inactive", False, det
    det["reason"] = "confirmed" if i - anchor_idx == need else "holding"
    return "active", i - anchor_idx == need, det


def duration_state(i: int, decline_idx: int, params: dict) -> tuple[bool, dict]:
    """持续时间：距下跌起点的完成周数 ≥ duration_weeks。"""
    need = params["exhaustion"]["duration_weeks"]
    elapsed = i - decline_idx
    det = {
        "decline_week_index": decline_idx,
        "elapsed_weeks": elapsed,
        "required_weeks": need,
    }
    cond = elapsed >= need
    det["reason"] = "condition_met" if cond else "condition_not_met"
    return cond, det


def is_pivot_low(weeks: list[WeekBar], k: int, side: int) -> bool:
    """pivot low：严格低于左右各 side 个完成周（窗口内其余 2×side 周）。"""
    if k - side < 0 or k + side >= len(weeks):
        return False
    low = weeks[k].low
    return all(weeks[j].low > low for j in range(k - side, k + side + 1) if j != k)


def divergence_state(weeks: list[WeekBar],
                     indicators: dict[str, tuple[float | None, float | None]],
                     i: int, params: dict) -> tuple[bool, dict]:
    """周线底背离：只在 pivot 确认周（pivot 周 + side 周）判定，不回填。"""
    d = params["exhaustion"]["divergence"]
    side = d["pivot_side_weeks"]
    lookback = d["lookback_weeks"]
    det: dict = {"pivot_side_weeks": side, "lookback_weeks": lookback}
    p = i - side  # 本周被确认的候选 pivot 周
    if p < side:
        det["reason"] = "insufficient_history"
        return False, det
    det["candidate_pivot_week"] = weeks[p].week_end_date
    if not is_pivot_low(weeks, p, side):
        det["reason"] = "no_pivot_confirmed"
        return False, det
    # 截至本周全部已确认 pivot（k + side <= i 天然满足，因 k <= p）
    pivots = [k for k in range(side, p + 1) if is_pivot_low(weeks, k, side)]
    det["confirmed_pivots"] = [weeks[k].week_end_date for k in pivots]
    if len(pivots) < 2:
        det["reason"] = "single_pivot"
        return False, det
    p1, p2 = pivots[-2], pivots[-1]
    if i - p1 > lookback:
        det["reason"] = "outside_lookback"
        return False, det
    rsi1, hist1 = indicators.get(weeks[p1].week_end_date, (None, None))
    rsi2, hist2 = indicators.get(weeks[p2].week_end_date, (None, None))
    det.update({
        "pivot_prev": {"week": weeks[p1].week_end_date, "close": weeks[p1].close,
                       "rsi12": rsi1, "macd_hist": hist1},
        "pivot_curr": {"week": weeks[p2].week_end_date, "close": weeks[p2].close,
                       "rsi12": rsi2, "macd_hist": hist2},
        "confirm_week": weeks[i].week_end_date,
    })
    price_lower = weeks[p2].close < weeks[p1].close
    rsi_div = rsi1 is not None and rsi2 is not None and rsi2 > rsi1
    hist_div = hist1 is not None and hist2 is not None and hist2 > hist1
    det["price_lower"] = price_lower
    det["rsi_divergence"] = rsi_div
    det["macd_hist_divergence"] = hist_div
    trig = price_lower and (rsi_div or hist_div)
    det["reason"] = "condition_met" if trig else "condition_not_met"
    return trig, det


# ---------------------------------------------------------------- 逐周编排

def _episode_end_indices(weeks: list[WeekBar], steps) -> list[int | None]:
    """每个 as_of 所在 episode（恐慌锚点身份连续段）的首个结束周索引。

    结束周 = 段内首个收盘 > 下跌起点收盘的完成周；无则 None。
    """
    n = len(weeks)
    ends: list[int | None] = [None] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and steps[j + 1].panic.key == steps[i].panic.key:
            j += 1
        decline = steps[i].decline
        if decline is not None:
            for k in range(i, j + 1):
                if weeks[k].close > decline.adjusted_price:
                    for m in range(k, j + 1):
                        ends[m] = k
                    break
        i = j + 1
    return ends


def compute_signal_rows(
    weeks: list[WeekBar],
    indicators: dict[str, tuple[float | None, float | None]],
    steps,
    params: dict,
    active_until_project: Callable[[int], str | None] | None = None,
) -> list[dict]:
    """逐周生成五项信号的 signal_facts 行（不含 symbol/run 字段）。

    steps：anchors.compute_anchor_timeline 结果（panic_anchor_id 已回填时可带入行）。
    active_until_project(目标周索引) → 该周 week_end_date（或日历任历投影）；
    为 None 时活跃截止一律存 NULL。
    """
    n = len(weeks)
    active_panic_weeks = params["exhaustion"]["active_weeks"]["panic"]
    active_div_weeks = params["exhaustion"]["active_weeks"]["divergence"]
    span_p = active_panic_weeks - 1   # 含确认周共 N 周 → 确认周索引 t 活跃至 t+N-1
    span_d = active_div_weeks - 1

    panic_trig = [panic_condition(weeks, i, params) for i in range(n)]
    div_eval = [divergence_state(weeks, indicators, i, params) for i in range(n)]
    div_trig = [t for t, _ in div_eval]
    ends = _episode_end_indices(weeks, steps)

    def project(idx: int) -> str | None:
        if idx < n:
            return weeks[idx].week_end_date
        if active_until_project is not None:
            return active_until_project(idx)
        return None

    def anchor_brief(step) -> dict:
        a = step.panic
        return {
            "anchor_type": a.anchor_type,
            "anchor_date": a.trade_date,
            "anchor_week": weeks[a.week_index].week_end_date,
            "anchor_adjusted_price": a.adjusted_price,
            "anchor_raw_price": a.raw_price,
            "is_fallback": a.is_fallback,
            "decline_date": step.decline.trade_date if step.decline else None,
            "decline_adjusted_close": (
                step.decline.adjusted_price if step.decline else None),
        }

    rows: list[dict] = []
    for i, w in enumerate(weeks):
        step = steps[i]
        end_idx = ends[i]
        ended = end_idx is not None and i >= end_idx

        def finish(signal, state, triggered, active_until, details):
            details["anchor"] = anchor_brief(step)
            if ended and state == "active":
                state = "inactive"
                triggered = False  # episode 结束周清触发残留（trigger 周与结束周可重合）
                details["reason"] = "episode_ended"
                end_week = weeks[end_idx].week_end_date
                if active_until is None or active_until > end_week:
                    active_until = end_week  # 活跃截止不晚于 episode 结束周
            details["episode_end_week"] = (
                weeks[end_idx].week_end_date if end_idx is not None else None)
            rows.append({
                "observed_on": w.week_end_date,
                "signal": signal,
                "state": state,
                "anchor_id": step.panic_anchor_id,
                "triggered": int(triggered),
                "active_until": active_until,
                "details": details,
            })

        # ---- 1. 恐慌型（确认周起活跃 4 个完成周，同锚点内）
        t = None
        for k in range(max(0, i - span_p), i + 1):
            if panic_trig[k] and steps[k].panic.key == step.panic.key:
                t = k
        det = {"triggered_this_week": panic_trig[i]}
        if panic_trig[i]:
            det.update(candle_metrics(w))
        if t is not None:
            det["reason"] = "condition_met" if panic_trig[i] else "holding_active"
            det["trigger_week"] = weeks[t].week_end_date
            active_until = project(t + span_p)
            if ended and end_idx <= t + span_p:
                active_until = weeks[end_idx].week_end_date
            finish("panic", "active", panic_trig[i] and t == i, active_until, det)
        else:
            det["reason"] = "condition_not_met"
            finish("panic", "inactive", False, None, det)

        # ---- 2. 干涸型（条件满足才活跃）
        if step.decline is None:
            finish("dry_up", "inactive", False, None,
                   {"reason": "no_decline_start"})
        else:
            cond, det = dryup_state(weeks, i, step.decline.week_index, params)
            if cond is None:
                finish("dry_up", "inactive", False, None, det)
            else:
                finish("dry_up", "active" if cond else "inactive", bool(cond),
                       w.week_end_date if cond else None, det)

        # ---- 3. 三周不创新低（再次创新低前持续活跃）
        st, trig, det = nnl_state(weeks, i, step.panic.week_index,
                                  step.panic.adjusted_price, params)
        finish("no_new_low_3w", st, trig, None, det)

        # ---- 4. 周线底背离（确认周起活跃 4 个完成周，同锚点内）
        t = None
        for k in range(max(0, i - span_d), i + 1):
            if div_trig[k] and steps[k].panic.key == step.panic.key:
                t = k
        _, det = div_eval[i]
        det = dict(det)
        if t is not None:
            det["reason"] = "condition_met" if div_trig[i] else "holding_active"
            det["trigger_week"] = weeks[t].week_end_date
            active_until = project(t + span_d)
            if ended and end_idx <= t + span_d:
                active_until = weeks[end_idx].week_end_date
            finish("divergence", "active", div_trig[i] and t == i,
                   active_until, det)
        else:
            finish("divergence", "inactive", False, None, det)

        # ---- 5. 持续时间（episode 内持续活跃）
        if step.decline is None:
            finish("duration", "inactive", False, None,
                   {"reason": "no_decline_start"})
        else:
            cond, det = duration_state(i, step.decline.week_index, params)
            det["decline_week"] = weeks[step.decline.week_index].week_end_date
            trig = cond and det["elapsed_weeks"] == params["exhaustion"]["duration_weeks"]
            finish("duration", "active" if cond else "inactive", trig, None, det)
    return rows


# ---------------------------------------------------------------- 活跃计数（供档位触发用）

def count_active_signals(conn: sqlite3.Connection, symbol: str,
                         week_end_date: str,
                         min_active: int | None = None) -> dict:
    """同一 anchor_id 下当前完成周活跃衰竭信号数（§5.3 "≥2 项"口径）。

    按 anchor_id 分组统计各组 distinct active 信号数，任一组 ≥ min_active 即
    meets_min=True；by_anchor 携带各 anchor 明细。顶层 active_count/
    active_signals/anchor_id 取活跃信号最多的一组（兼容旧调用方）。
    """
    placeholders = ", ".join("?" * len(WEEKLY_SIGNALS))
    rows = conn.execute(
        f"""
        SELECT signal, state, anchor_id FROM signal_facts
        WHERE symbol = ? AND observed_on = ? AND signal IN ({placeholders})
        """,
        (symbol, week_end_date, *WEEKLY_SIGNALS),
    ).fetchall()
    anchor_ids = sorted({r["anchor_id"] for r in rows},
                        key=lambda a: (a is None, a))
    groups: dict[int | None, set] = {a: set() for a in anchor_ids}
    for r in rows:
        if r["state"] == "active":
            groups[r["anchor_id"]].add(r["signal"])
    by_anchor = [
        {
            "anchor_id": a,
            "active_count": len(sigs),
            "active_signals": sorted(sigs),
            "meets_min": (len(sigs) >= min_active) if min_active is not None else None,
        }
        for a, sigs in groups.items()
    ]
    best = max(by_anchor, key=lambda g: g["active_count"], default=None)
    return {
        "symbol": symbol,
        "week_end_date": week_end_date,
        "anchor_id": best["anchor_id"] if best else None,
        "active_count": best["active_count"] if best else 0,
        "active_signals": best["active_signals"] if best else [],
        "meets_min": (any(g["meets_min"] for g in by_anchor)
                      if min_active is not None else None),
        "by_anchor": by_anchor,
    }
