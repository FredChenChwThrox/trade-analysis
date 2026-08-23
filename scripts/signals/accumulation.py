"""吸筹形态状态机（设计 §5.5，方法来源《如何看出主力吸筹》三阶段框架）。

把"放量破位 → 缩量横盘 → 试盘 → 放量突破确认"做成日线级确定性状态机，
结果写 signal_facts（signal="accumulation"），每日一行，作为日报观察点。
不进排期卡触发逻辑，不改衰竭信号"≥2 项"口径。

状态流转（terminal 后次日回 idle，同 right_side 纪律）：

    idle --放量破位--> watching --横盘确认--> consolidating
        --放量阳线突破区间上沿--> confirmed（terminal）
        --收盘跌破区间下沿 / 超期--> failed（terminal）

判定（全部只用当日及之前数据，无未来函数；价用复权、量用 volume_raw/share_factor，
与指标层 §4.1 口径一致）：

1. 放量破位（idle 内每日检查）：单日跌幅 ≥ drop_pct（默认 5%），且当日调整量
   ≥ 前 vol_ma_days（默认 20，shift(1)）日均量 × vol_multiple（默认 2.0），
   且收盘创 new_low_days（默认 60）日新低。样本不足不判定（reason
   insufficient_history，§4.1 不拿短窗口冒充）。
2. 缩量横盘（watching）：破位后满 min_days（默认 10）个交易日起，对
   (破位日, 当日] 窗口判定——窗口振幅 (max(high)−min(low))/min(low)
   ≤ range_pct（默认 15%），窗口均量 ≤ 破位基数均量 × vol_ratio（默认 0.8），
   且 MA5/10/20 粘合 (max−min)/MA20 ≤ ma_convergence_pct（默认 5%，MA 缺失
   则当日不判定）。三条件同时满足 → consolidating，箱体取窗口收盘价
   [min(close), max(close)]（收盘价定界，避免影线毛刺）。破位后超过 max_days
   （默认 120）未确认 → failed(expired_no_consolidation)。确认前收盘低于破位日
   收盘 ×(1−continue_drop_pct) → 仍在下跌，继续等待（reason still_falling）。
3. 试盘（consolidating 内每日计数）：当日振幅 ≥ min_amplitude_pct（默认 3%），
   上影 ≥ 全日振幅 × upper_shadow_range_pct（默认 50%），且调整量 ≥ 前 20 日
   均量 × probe.vol_multiple（默认 1.5）→ probe_today=True，probe_count+1
   （计数写入 details，不产生状态转换）。注：分时/盘口数据缺失，本判定只是
   日 K 级代理，精度低于原方法（§5.5 限制标注）。
4. 确认：收盘 > 箱体上沿，且收阳（close > open），且调整量 ≥ 前 20 日均量
   × confirm.vol_multiple（默认 1.5）→ confirmed（terminal，triggered=1）。
5. 失效：收盘 < 箱体下沿 → failed(box_broken)；横盘超过
   max_consolidation_days（默认 120）未确认 → failed(expired_consolidation)。

⚠️ 全部参数为第一版默认值，需人工核对数周后才可调整（同 §5.2 纪律）；
形态识别误报天然偏高（下跌中继与吸筹前期同形），输出仅为观察点。

派生表重算语义：DELETE 本模块 signal 行后全量重插（§2.2 第 3 类），
run 记录写 pipeline_runs 阶段 accumulation。

CLI：
    uv run python -m scripts.signals.accumulation <symbol> [--as-of D] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals.common import RULE_VERSION, load_params

SIGNAL = "accumulation"

# 状态取值（signal_facts.state）
ST_IDLE = "idle"
ST_WATCHING = "watching"            # 放量破位已见，等待缩量横盘确认
ST_CONSOLIDATING = "consolidating"  # 缩量横盘中（试盘计数）
ST_CONFIRMED = "confirmed"          # terminal：放量阳线突破箱体上沿
ST_FAILED = "failed"                # terminal：跌破箱体下沿 / 超期


@dataclass
class AccumulationResult:
    symbol: str = ""
    run_id: str = ""
    as_of: str = ""
    config_hash: str = ""
    rule_version: str = RULE_VERSION
    status: str = "ok"           # ok / incomplete / degraded
    reason: str = ""
    days: int = 0
    facts_written: int = 0
    latest: dict = field(default_factory=dict)      # as_of 当日状态明细
    transitions: list[dict] = field(default_factory=list)  # 全部状态转换事件
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"{self.symbol} run_id={self.run_id} rule_version={self.rule_version} "
            f"status={self.status}" + (f"（{self.reason}）" if self.reason else ""),
            f"as_of={self.as_of} 监测日 {self.days} 天，signal_facts {self.facts_written} 行",
        ]
        cur = self.latest
        if cur:
            lines.append(f"当前状态: {cur.get('state')}（reason={cur.get('reason')}）")
            if cur.get("breakdown_date"):
                lines.append(
                    f"  破位日 {cur['breakdown_date']}，破位基数均量 "
                    f"{cur.get('breakdown_vol_base')}")
            if cur.get("box_low") is not None:
                lines.append(
                    f"  箱体 [{cur.get('box_low')}, {cur.get('box_high')}]（复权收盘口径），"
                    f"试盘计数 {cur.get('probe_count', 0)} 次")
        if self.transitions:
            lines.append(f"状态转换 {len(self.transitions)} 次（最近 10 次）:")
            for t in self.transitions[-10:]:
                lines.append(f"  {t['observed_on']}: -> {t['state']}（{t['reason']}）")
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------- 数据加载

def _load_bars(conn: sqlite3.Connection, symbol: str, as_of: str) -> tuple[list[dict], list[str]]:
    """复权价 + 调整量日线（§4.1 口径：价 ×price_adj_factor，量 ÷share_factor）。

    OHLCV 关键字段缺失的行拒绝参与计算（§2.5 不猜，缺失不当 0），
    返回 (bars, skipped_dates)。
    """
    rows = conn.execute(
        """
        SELECT trade_date, open_raw, high_raw, low_raw, close_raw, volume_raw,
               price_adj_factor, share_factor
        FROM daily_bars WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date
        """,
        (symbol, as_of),
    ).fetchall()
    bars, skipped = [], []
    for r in rows:
        if any(r[k] is None for k in
               ("open_raw", "high_raw", "low_raw", "close_raw", "volume_raw")):
            skipped.append(r["trade_date"])
            continue
        f = r["price_adj_factor"] or 1.0
        sf = r["share_factor"] or 1.0
        bars.append({
            "trade_date": r["trade_date"],
            "open": r["open_raw"] * f,
            "high": r["high_raw"] * f,
            "low": r["low_raw"] * f,
            "close": r["close_raw"] * f,
            "volume": r["volume_raw"] / sf,
        })
    return bars, skipped


def _load_ma(conn: sqlite3.Connection, symbol: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT trade_date, ma5, ma10, ma20 FROM indicators_daily WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    return {r["trade_date"]: {"ma5": r["ma5"], "ma10": r["ma10"], "ma20": r["ma20"]}
            for r in rows}


# ---------------------------------------------------------------- 单项判定（纯函数）

def vol_base(bars: list[dict], i: int, window: int) -> float | None:
    """前 window 日（shift(1)，不含当日）调整量均值；样本不足返回 None。"""
    if i < window:
        return None
    return sum(b["volume"] for b in bars[i - window:i]) / window


def is_breakdown(bars: list[dict], i: int, base: float, p: dict) -> tuple[bool, dict]:
    """放量破位：单日大跌 + 放量 + 收盘创 N 日新低。"""
    b = p["breakdown"]
    n = b["new_low_days"]
    det = {"drop_pct_threshold": b["drop_pct"], "vol_multiple": b["vol_multiple"],
           "new_low_days": n}
    if i < 1 or i < n - 1:
        det["reason"] = "insufficient_history"
        return False, det
    prev_close = bars[i - 1]["close"]
    if prev_close <= 0:
        # 前收 ≤0（脏数据）不猜，当日涨跌幅不判定（§2.5）
        det["reason"] = "invalid_prev_close"
        return False, det
    chg = bars[i]["close"] / prev_close - 1.0
    is_new_low = bars[i]["close"] <= min(x["close"] for x in bars[i - n + 1:i + 1])
    vol_ok = base is not None and bars[i]["volume"] >= b["vol_multiple"] * base
    det.update({"pct_chg": chg, "is_new_low": is_new_low, "vol_base": base,
                "volume": bars[i]["volume"], "vol_ok": vol_ok})
    cond = chg <= -b["drop_pct"] and vol_ok and is_new_low
    det["reason"] = "condition_met" if cond else "condition_not_met"
    return cond, det


def consolidation_state(bars: list[dict], i: int, breakdown_idx: int,
                        breakdown_vol_base: float, ma_row: dict | None,
                        p: dict) -> tuple[bool | None, dict]:
    """缩量横盘三条件。返回 (cond, details)；cond=None 表示当日不可判定。"""
    c = p["consolidation"]
    win = bars[breakdown_idx + 1:i + 1]
    lo, hi = min(b["low"] for b in win), max(b["high"] for b in win)
    range_pct = (hi - lo) / lo if lo > 0 else None
    win_vol_mean = sum(b["volume"] for b in win) / len(win)
    det = {
        "window_start": win[0]["trade_date"], "window_end": win[-1]["trade_date"],
        "window_days": len(win),
        "range_pct": range_pct, "range_pct_threshold": c["range_pct"],
        "window_vol_mean": win_vol_mean,
        "vol_ratio_threshold": c["vol_ratio"],
        "breakdown_vol_base": breakdown_vol_base,
        "ma_convergence_threshold": c["ma_convergence_pct"],
    }
    mas = [v for v in ((ma_row or {}).get(k) for k in ("ma5", "ma10", "ma20"))
           if v is not None]
    if len(mas) < 3 or not (ma_row or {}).get("ma20"):
        det["reason"] = "missing_ma"
        return None, det
    ma_spread = (max(mas) - min(mas)) / ma_row["ma20"] if ma_row["ma20"] else None
    det["ma_spread"] = ma_spread
    cond = (range_pct is not None and range_pct <= c["range_pct"]
            and win_vol_mean <= c["vol_ratio"] * breakdown_vol_base
            and ma_spread is not None and ma_spread <= c["ma_convergence_pct"])
    det["reason"] = "condition_met" if cond else "condition_not_met"
    if cond:
        det["box_low"] = min(b["close"] for b in win)
        det["box_high"] = max(b["close"] for b in win)
    return cond, det


def is_probe(bar: dict, prev_close: float, base: float | None,
             p: dict) -> tuple[bool, dict]:
    """试盘（日 K 代理）：足够振幅 + 长上影 + 放量。"""
    pr = p["probe"]
    rng = bar["high"] - bar["low"]
    amp = rng / prev_close if prev_close > 0 else 0.0
    upper = bar["high"] - max(bar["open"], bar["close"])
    upper_pct = upper / rng if rng > 0 else 0.0
    vol_ok = base is not None and bar["volume"] >= pr["vol_multiple"] * base
    det = {"amplitude": amp, "upper_shadow_range_pct": upper_pct,
           "vol_base": base, "volume": bar["volume"], "vol_ok": vol_ok,
           "thresholds": dict(pr)}
    cond = (amp >= pr["min_amplitude_pct"]
            and upper_pct >= pr["upper_shadow_range_pct"] and vol_ok)
    det["reason"] = "condition_met" if cond else "condition_not_met"
    return cond, det


# ---------------------------------------------------------------- 全量重算

def run_accumulation(
    conn: sqlite3.Connection,
    symbol: str,
    as_of: str | None = None,
    *,
    run_id: str | None = None,
    params: dict | None = None,
    config_hash: str | None = None,
) -> AccumulationResult:
    """重算该股吸筹形态信号（调用方负责事务/提交）。"""
    started_at = utc_now()
    if params is None or config_hash is None:
        params, config_hash = load_params()
    if as_of is None:
        r = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars WHERE symbol = ?", (symbol,)
        ).fetchone()
        as_of = r["d"] if r and r["d"] else None
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"accumulation_{symbol}_{now_compact}"
    res = AccumulationResult(symbol=symbol, run_id=run_id, as_of=as_of or "",
                             config_hash=config_hash)
    if as_of is None:
        res.status, res.reason = "incomplete", "no_daily_bars"
        return res

    p = params["accumulation"]
    bars, skipped_rows = _load_bars(conn, symbol, as_of)
    ma_map = _load_ma(conn, symbol)

    def mark_degraded(reason: str, note: str) -> None:
        if res.status == "ok":
            res.status, res.reason = "degraded", reason
        res.notes.append(note)

    if skipped_rows:  # OHLCV 缺失行被剔除，整体仍出结果但记 degraded（§2.5）
        mark_degraded(
            "missing_ohlcv_rows",
            f"跳过 OHLCV 缺失行 {len(skipped_rows)} 天: {', '.join(skipped_rows[:10])}")
    now = utc_now()
    conn.execute("DELETE FROM signal_facts WHERE symbol = ? AND signal = ?",
                 (symbol, SIGNAL))

    state = ST_IDLE
    breakdown_idx: int | None = None
    breakdown_vol_base: float | None = None
    consolidation_start_idx: int | None = None  # 进入 consolidating 的索引（expired 起算点，§5.4c）
    box: dict = {}
    probe_count = 0
    terminal_pending_idle = False  # terminal 日后次日回 idle

    def write(day: str, st: str, triggered: int, details: dict) -> None:
        conn.execute(
            """
            INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
                triggered, active_until, details_json, run_id, rule_version,
                config_hash, created_at)
            VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (symbol, day, SIGNAL, st, triggered,
             json.dumps(details, ensure_ascii=False, sort_keys=True),
             run_id, RULE_VERSION, config_hash, now),
        )
        res.facts_written += 1
        res.days += 1

    for i, bar in enumerate(bars):
        day = bar["trade_date"]
        base = vol_base(bars, i, p["vol_ma_days"])
        det: dict = {"vol_base": base}
        if breakdown_idx is not None:
            det["breakdown_date"] = bars[breakdown_idx]["trade_date"]
            det["breakdown_vol_base"] = breakdown_vol_base
        if box:
            det.update(box)
            det["probe_count"] = probe_count
        triggered = 0

        if terminal_pending_idle:  # terminal 次日重新扫描
            state, terminal_pending_idle = ST_IDLE, False
            breakdown_idx, breakdown_vol_base, box, probe_count = None, None, {}, 0
            consolidation_start_idx = None
            det = {"vol_base": base}

        if state == ST_IDLE:
            cond, bd = is_breakdown(bars, i, base, p)
            det["breakdown_check"] = bd
            if cond:
                state = ST_WATCHING
                breakdown_idx, breakdown_vol_base = i, base
                det["breakdown_date"] = day
                det["breakdown_vol_base"] = base
                det["reason"] = "breakdown_detected"
                triggered = 1
            else:
                det["reason"] = bd["reason"]
                if bd["reason"] == "invalid_prev_close":  # 前收 ≤0 不猜（§2.5）
                    mark_degraded("invalid_prev_close",
                                  f"{day} 前收 ≤0，当日破位不判定")

        elif state == ST_WATCHING:
            assert breakdown_idx is not None
            days_since = i - breakdown_idx
            det["days_since_breakdown"] = days_since
            if days_since > p["consolidation"]["max_days"]:
                state = ST_FAILED
                det["reason"] = "expired_no_consolidation"
                triggered = 1
                terminal_pending_idle = True
            elif bar["close"] < (bars[breakdown_idx]["close"]
                                 * (1 - p["continue_drop_pct"])):
                det["reason"] = "still_falling"
            elif days_since >= p["consolidation"]["min_days"]:
                cond, cd = consolidation_state(
                    bars, i, breakdown_idx, breakdown_vol_base,
                    ma_map.get(day), p)
                det["consolidation_check"] = cd
                if cond:
                    state = ST_CONSOLIDATING
                    consolidation_start_idx = i  # expired_consolidation 从此起算
                    box = {"box_low": cd["box_low"], "box_high": cd["box_high"]}
                    det.update(box)
                    det["probe_count"] = 0
                    det["reason"] = "consolidation_confirmed"
                    triggered = 1
                else:
                    det["reason"] = cd["reason"]
            else:
                det["reason"] = "waiting_min_days"

        elif state == ST_CONSOLIDATING:
            probe, pd_ = is_probe(bar, bars[i - 1]["close"] if i >= 1 else bar["close"],
                                  base, p)
            det["probe_check"] = pd_
            if probe:
                probe_count += 1
                det["probe_today"] = True
                det["probe_count"] = probe_count
            # 横盘窗口从进入 consolidating 起算（§5.4c），不从破位日起算
            days_consol = (i - consolidation_start_idx
                           if consolidation_start_idx is not None else 0)
            det["days_consolidating"] = days_consol
            if bar["close"] < box["box_low"]:
                state = ST_FAILED
                det["reason"] = "box_broken"
                triggered = 1
                terminal_pending_idle = True
            elif (bar["close"] > box["box_high"] and bar["close"] > bar["open"]
                  and base is not None
                  and bar["volume"] >= p["confirm"]["vol_multiple"] * base):
                state = ST_CONFIRMED
                det["reason"] = "breakout_confirmed"
                triggered = 1
                terminal_pending_idle = True
            elif days_consol > p["max_consolidation_days"]:
                state = ST_FAILED
                det["reason"] = "expired_consolidation"
                triggered = 1
                terminal_pending_idle = True
            else:
                det["reason"] = ("probe_detected" if probe else "holding")

        write(day, state, triggered, det)
        if triggered:
            res.transitions.append(
                {"observed_on": day, "state": state, "reason": det["reason"]})

    res.latest = {
        "trade_date": bars[-1]["trade_date"], "state": state,
        "reason": det.get("reason"),
        "breakdown_date": det.get("breakdown_date"),
        "breakdown_vol_base": det.get("breakdown_vol_base"),
        "box_low": box.get("box_low"), "box_high": box.get("box_high"),
        "probe_count": probe_count,
    } if bars else {}

    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, app_version,
            status, error, started_at, finished_at)
        VALUES (?, 'accumulation', ?, ?, NULL, ?, ?, NULL, 'success', NULL, ?, ?)
        """,
        (run_id, utc_now(), as_of, config_hash, RULE_VERSION, started_at, utc_now()),
    )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.signals.accumulation")
    parser.add_argument("symbol")
    parser.add_argument("--as-of", default=None, help="数据截止交易日，默认最新 bar")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        with conn:  # DELETE + 重插 + run 记录同一事务（§4.3）
            res = run_accumulation(conn, args.symbol, as_of=args.as_of)
        print(res)
        return 0 if res.status == "ok" else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
