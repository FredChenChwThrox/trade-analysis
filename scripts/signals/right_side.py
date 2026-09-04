"""右侧确认状态机（D2.3，设计 §5.4、§5.1、§2.5）。

状态机（关键位 trigger_level 从当前生效卡片的 right_side_trigger_json 读，
不由日报临时猜）：

```text
idle
  -> waiting_retest：收盘 ≥ 关键位 ×(1+breakout_pct)（默认 1%），且当日调整后
     成交量 ≥ 此前 vol_ma_days（默认 20，shift(1) 不含当日，§4.1）个交易日的
     调整后均量 × vol_multiple（默认 2.0）
  -> confirmed：breakout 后 retest_window_days（默认 10）个交易日内，盘中最低
     回踩到关键位 ×(1+retest_band_pct)（默认上 2%）以内，且收盘 ≥ 关键位
     ×(1−retest_hold_pct)（默认 1%）
  -> invalidated：等待期间收盘 ≤ 关键位 ×(1−invalidate_pct)（默认 1%）
  -> expired：retest_window_days 个交易日内未发生合格回踩
confirmed 之后（2026-08-30 起，signals_v2）：
  -> holding：卡片带 stop_level 时进入持仓跟踪，逐日落行（state='holding'，
     details 含止损位、现价距止损距离、确认日与跟踪天数）——修复"确认后失声"：
     此前 confirmed 直接回 idle，持仓期破线无任何事实行与日报决策点
  -> stopped_out：跟踪期间收盘 ≤ stop_level（跌破用 ≤，与证伪线语义一致；
     stop_level 已是含缓冲的判定线，不再加容差、不要求连续日数），随后回 idle
  卡片无 stop_level：confirmed 后直接回 idle（§2.5 无线不猜），transition
  details 记 tracking=no_stop_level
```

边界语义锁定：突破/回踩保持用 ≥，跌破用 ≤（与 daily_watch 证伪线一致）。
等待期内判定顺序：invalidated > confirmed > expired。terminal 状态次日起
回到 idle 可开启新一轮 episode。量能为口径一致的调整后量
（volume_raw ÷ share_factor，与周线一致）；均量样本不足 20 日时当日不判定
突破（不拿短窗口冒充，§4.1），保持原状态并在 details 记原因。

每次状态转换写 signal_facts（signal='right_side'，observed_on=转换日，
state=新状态，triggered=1，details 含起始/截止日、关键位、容差与成交量明细，
§5.4）；holding 跟踪行逐日写（triggered=0）。逐版本生效区间计算（§5.1），
版本切换时状态机重置为 idle（关键位不同）。公司行为冻结期间挂起：
冻结日不参与判定、不推进窗口（§5.4b）。

无 active 卡片或卡片无触发位：保持 idle，写 signal_facts 行记 incomplete
（reason=no_active_card / no_trigger_level，§2.5）。

CLI：
    uv run python -m scripts.signals.right_side <symbol> [--as-of D] [--db PATH]
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
from scripts.signals.common import RULE_VERSION, load_params

RIGHT_SIDE_SIGNAL = "right_side"
STATES = ("idle", "waiting_retest", "confirmed", "holding", "stopped_out",
          "invalidated", "expired")


@dataclass
class RightSideResult:
    symbol: str = ""
    run_id: str = ""
    as_of: str = ""
    config_hash: str = ""
    rule_version: str = RULE_VERSION
    status: str = "ok"               # ok / incomplete
    reason: str = ""
    episodes: int = 0                # waiting_retest 进入次数
    transitions: list[dict] = field(default_factory=list)
    current_state: str = "idle"
    current_level: str | None = None
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"{self.symbol} run_id={self.run_id} rule_version={self.rule_version} "
            f"status={self.status}" + (f"（{self.reason}）" if self.reason else ""),
            f"as_of={self.as_of} 当前状态={self.current_state} "
            f"关键位={self.current_level} 转换 {len(self.transitions)} 次",
        ]
        for t in self.transitions[-10:]:
            lines.append(f"  {t['observed_on']}: {t['from_state']} -> {t['to_state']}"
                         f"（{t.get('reason', '')}）")
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------- 纯状态机

def evaluate_segment(days: list[dict], level: Decimal, p: dict,
                     stop: Decimal | None = None
                     ) -> tuple[list[dict], list[dict], str]:
    """单卡片版本生效区间上跑状态机（纯函数，便于测试）。

    days: [{"trade_date", "close"(Decimal), "low"(Decimal), "volume_adj"(float|None),
            "vol_base"(float|None)}]，vol_base=None 表示均量样本不足。
    stop: 卡片 right_side_trigger.stop_level；None = confirmed 后不跟踪直接回 idle。
    返回 (transitions, track_rows, final_state)；transition 含 from_state/to_state/
    observed_on/reason 与全部判定明细；track_rows 为 holding 期间逐日跟踪行
    （含止损位与距止损距离）。
    """
    breakout_line = level * (Decimal("1") + Decimal(str(p["breakout_pct"])))
    band_line = level * (Decimal("1") + Decimal(str(p["retest_band_pct"])))
    hold_line = level * (Decimal("1") - Decimal(str(p["retest_hold_pct"])))
    inv_line = level * (Decimal("1") - Decimal(str(p["invalidate_pct"])))
    window = int(p["retest_window_days"])
    vol_mult = float(p["vol_multiple"])

    base_det = {
        "trigger_level": str(level),
        "breakout_pct": p["breakout_pct"], "breakout_line": str(breakout_line),
        "retest_band_pct": p["retest_band_pct"], "retest_band_line": str(band_line),
        "retest_hold_pct": p["retest_hold_pct"], "hold_line": str(hold_line),
        "invalidate_pct": p["invalidate_pct"], "invalidate_line": str(inv_line),
        "retest_window_days": window,
        "vol_ma_days": p["vol_ma_days"], "vol_multiple": vol_mult,
    }

    transitions: list[dict] = []
    track_rows: list[dict] = []
    state = "idle"
    breakout_day: str | None = None
    confirm_day: str | None = None
    waited = 0
    held = 0

    for d in days:
        det = dict(base_det)
        det.update({"trade_date": d["trade_date"], "close_raw": str(d["close"]),
                    "low_raw": str(d["low"]), "volume_adj": d["volume_adj"],
                    "vol_base": d["vol_base"]})
        if state == "idle":
            if d["vol_base"] is None:
                continue  # 均量样本不足，不判定（§4.1）
            breakout = (d["close"] >= breakout_line
                        and d["volume_adj"] is not None
                        and d["volume_adj"] >= vol_mult * d["vol_base"])
            if breakout:
                det.update({
                    "from_state": "idle", "to_state": "waiting_retest",
                    "observed_on": d["trade_date"],
                    "reason": "breakout_with_volume",
                    "window_start": d["trade_date"],
                    "volume_ratio": (d["volume_adj"] / d["vol_base"]
                                     if d["vol_base"] else None),
                })
                transitions.append(det)
                state, breakout_day, waited = "waiting_retest", d["trade_date"], 0
        elif state == "waiting_retest":
            waited += 1
            det["days_waited"] = waited
            det["window_start"] = breakout_day
            if d["close"] <= inv_line:
                det.update({"from_state": "waiting_retest", "to_state": "invalidated",
                            "observed_on": d["trade_date"],
                            "reason": "close_below_invalidate_line"})
                transitions.append(det)
                state = "idle"
            elif d["low"] <= band_line and d["close"] >= hold_line:
                det["tracking"] = "holding" if stop is not None else "no_stop_level"
                det.update({"from_state": "waiting_retest", "to_state": "confirmed",
                            "observed_on": d["trade_date"],
                            "reason": "retest_within_band_and_held"})
                transitions.append(det)
                if stop is not None:
                    state, confirm_day, held = "holding", d["trade_date"], 0
                else:
                    state = "idle"  # 无止损位不跟踪（§2.5：无线不猜）
            elif waited >= window:
                det.update({"from_state": "waiting_retest", "to_state": "expired",
                            "observed_on": d["trade_date"],
                            "reason": "no_qualified_retest_within_window",
                            "window_deadline": d["trade_date"]})
                transitions.append(det)
                state = "idle"
        elif state == "holding":  # confirmed 后按卡片 stop_level 逐日跟踪
            held += 1
            det.update({
                "from_state": "holding", "observed_on": d["trade_date"],
                "confirmed_on": confirm_day, "days_since_confirm": held,
                "stop_level": str(stop),
                "distance_to_stop_pct": float((d["close"] - stop) / stop),
            })
            if d["close"] <= stop:  # 跌破用 ≤；stop 已是判定线，不再加容差
                det.update({"to_state": "stopped_out",
                            "reason": "close_below_stop_level"})
                transitions.append(det)
                state = "idle"
            else:
                det.update({"to_state": "holding", "reason": "tracking"})
                track_rows.append(det)
        else:
            raise ValueError(f"unexpected right_side state: {state}")
    return transitions, track_rows, state


# ---------------------------------------------------------------- 全量重算

def run_right_side(
    conn: sqlite3.Connection,
    symbol: str,
    as_of: str | None = None,
    *,
    run_id: str | None = None,
    params: dict | None = None,
    config_hash: str | None = None,
) -> RightSideResult:
    """重算该股右侧确认状态机（调用方负责事务/提交）。"""
    started_at = utc_now()
    if params is None or config_hash is None:
        params, config_hash = load_params()
    if as_of is None:
        r = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars WHERE symbol = ?", (symbol,)
        ).fetchone()
        as_of = r["d"] if r and r["d"] else None
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"right_side_{symbol}_{now_compact}"
    res = RightSideResult(symbol=symbol, run_id=run_id, as_of=as_of or "",
                          config_hash=config_hash)
    if as_of is None:
        res.status, res.reason = "incomplete", "no_daily_bars"
        return res

    p = params["right_side"]
    rows = conn.execute(
        """
        SELECT trade_date, close_raw, low_raw, volume_raw, share_factor
        FROM daily_bars WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date
        """,
        (symbol, as_of),
    ).fetchall()
    # 调整后量 + 前 vol_ma_days 日均量（shift(1) 不含当日，§4.1；不足窗口为 None）
    days_all: list[dict] = []
    vols: list[float] = []
    n = int(p["vol_ma_days"])
    skipped_missing = 0
    for r in rows:
        # OHLCV 关键字段缺失：不当 0、不进 Decimal，跳过且不进均量窗口（§2.5）
        if r["close_raw"] is None or r["low_raw"] is None or r["volume_raw"] is None:
            skipped_missing += 1
            continue
        sf = r["share_factor"] or 1.0
        vol_adj = r["volume_raw"] / sf
        base = (sum(vols[-n:]) / n) if len(vols) >= n else None
        days_all.append({
            "trade_date": r["trade_date"],
            "close": Decimal(str(r["close_raw"])),
            "low": Decimal(str(r["low_raw"])),
            "volume_adj": vol_adj,
            "vol_base": base,
        })
        vols.append(vol_adj)

    versions = card_mod.load_card_versions(conn, symbol)
    suspensions = ca_mod.unresolved_suspensions(conn, symbol)
    frozen_from = min((s["ex_date"] for s in suspensions), default=None)

    now = utc_now()
    conn.execute(
        "DELETE FROM signal_facts WHERE symbol = ? AND signal = ?",
        (symbol, RIGHT_SIDE_SIGNAL),
    )

    current_state = "idle"
    current_level: str | None = None
    for card in versions:
        if card.trigger_level is None:
            continue
        seg = [d for d in days_all
               if card.covers(d["trade_date"])
               and (frozen_from is None or d["trade_date"] < frozen_from)]
        if not seg:
            continue
        transitions, track_rows, final_state = evaluate_segment(
            seg, card.trigger_level, p, stop=card.stop_level)
        # 转换行 triggered=1；holding 跟踪行 triggered=0；按 observed_on 合并落库
        rows = ([(t["observed_on"], t["to_state"], 1, t) for t in transitions]
                + [(r["observed_on"], "holding", 0, r) for r in track_rows])
        rows.sort(key=lambda x: x[0])
        for observed_on, state_name, trig, det in rows:
            conn.execute(
                """
                INSERT INTO signal_facts (symbol, observed_on, signal, state,
                    anchor_id, triggered, active_until, details_json, run_id,
                    rule_version, config_hash, created_at)
                VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (symbol, observed_on, RIGHT_SIDE_SIGNAL, state_name, trig,
                 json.dumps(det, ensure_ascii=False, sort_keys=True),
                 run_id, RULE_VERSION, config_hash, now),
            )
        for t in transitions:
            res.transitions.append({
                "observed_on": t["observed_on"], "from_state": t["from_state"],
                "to_state": t["to_state"], "reason": t["reason"],
            })
        res.episodes += sum(1 for t in transitions
                            if t["to_state"] == "waiting_retest")
        if card.stop_level is None and any(t["to_state"] == "confirmed"
                                           for t in transitions):
            res.notes.append(
                f"卡片 {card.card_version_id} 无 stop_level，confirmed 后不跟踪"
                "（§2.5：无线不猜）")
        # 最新版本段的结果为当前状态
        current_state = final_state
        current_level = str(card.trigger_level)

    res.current_state, res.current_level = current_state, current_level

    # ---- as_of 当日无生效卡/无触发位 → idle + incomplete（§2.5）
    # 生效判定走 §5.1 窗口语义（card_for_day 含 superseded 但窗口仍覆盖的版本），
    # 与逐日计算口径一致；不用 load_active_card 的 status='active' 口径，
    # 否则新旧卡交替空档期（旧卡 superseded、新卡未生效）会误报 no_active_card。
    active_card = card_mod.card_for_day(versions, as_of)
    if active_card is None or active_card.trigger_level is None:
        reason = (("no_active_card" if not versions else "card_not_effective_at_as_of")
                  if active_card is None else "no_trigger_level")
        conn.execute(
            """
            INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
                triggered, active_until, details_json, run_id, rule_version,
                config_hash, created_at)
            VALUES (?, ?, ?, 'idle', NULL, 0, NULL, ?, ?, ?, ?, ?)
            """,
            (symbol, as_of, RIGHT_SIDE_SIGNAL,
             json.dumps({"reason": reason}, ensure_ascii=False, sort_keys=True),
             run_id, RULE_VERSION, config_hash, now),
        )
        res.status, res.reason = "incomplete", reason
        res.current_state, res.current_level = "idle", None
    if frozen_from is not None and active_card is not None:
        res.notes.append(
            f"公司行为冻结中（ex_date={frozen_from} 起），状态机挂起不推进窗口（§5.4b）")
    if skipped_missing:
        # 缺失 bar 已跳过：结果降级为 incomplete（§2.5），run 记录落 degraded
        res.notes.append(
            f"{skipped_missing} 个交易日 OHLCV 关键字段缺失，跳过不判定（§2.5）")
        if res.status == "ok":
            res.status, res.reason = "incomplete", "missing_ohlcv_bars"

    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, card_version_id,
            status, error, started_at, finished_at)
        VALUES (?, 'right_side', ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (run_id, utc_now(), as_of, config_hash, RULE_VERSION,
         active_card.card_version_id if active_card else None,
         "success" if res.status == "ok" else "degraded", started_at, utc_now()),
    )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.signals.right_side")
    parser.add_argument("symbol")
    parser.add_argument("--as-of", default=None, help="数据截止交易日，默认最新 bar")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        with conn:
            res = run_right_side(conn, args.symbol, as_of=args.as_of)
        print(res)
        return 0 if res.status == "ok" else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
