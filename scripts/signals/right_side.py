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
```

边界语义锁定：突破/回踩保持用 ≥，跌破用 ≤（与 daily_watch 证伪线一致）。
等待期内判定顺序：invalidated > confirmed > expired。terminal 状态次日起
回到 idle 可开启新一轮 episode。量能为口径一致的调整后量
（volume_raw ÷ share_factor，与周线一致）；均量样本不足 20 日时当日不判定
突破（不拿短窗口冒充，§4.1），保持原状态并在 details 记原因。

每次状态转换写 signal_facts（signal='right_side'，observed_on=转换日，
state=新状态，details 含起始/截止日、关键位、容差与成交量明细，§5.4）。
逐版本生效区间计算（§5.1），版本切换时状态机重置为 idle（关键位不同）。
公司行为冻结期间挂起：冻结日不参与判定、不推进窗口（§5.4b）。

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
STATES = ("idle", "waiting_retest", "confirmed", "invalidated", "expired")


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

def evaluate_segment(days: list[dict], level: Decimal, p: dict) -> tuple[list[dict], str]:
    """单卡片版本生效区间上跑状态机（纯函数，便于测试）。

    days: [{"trade_date", "close"(Decimal), "low"(Decimal), "volume_adj"(float|None),
            "vol_base"(float|None)}]，vol_base=None 表示均量样本不足。
    返回 (transitions, final_state)；transition 含 from_state/to_state/observed_on/
    reason 与全部判定明细。
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
    state = "idle"
    breakout_day: str | None = None
    waited = 0

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
        else:  # waiting_retest
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
                det.update({"from_state": "waiting_retest", "to_state": "confirmed",
                            "observed_on": d["trade_date"],
                            "reason": "retest_within_band_and_held"})
                transitions.append(det)
                state = "idle"
            elif waited >= window:
                det.update({"from_state": "waiting_retest", "to_state": "expired",
                            "observed_on": d["trade_date"],
                            "reason": "no_qualified_retest_within_window",
                            "window_deadline": d["trade_date"]})
                transitions.append(det)
                state = "idle"
    return transitions, state


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
    for r in rows:
        sf = r["share_factor"] or 1.0
        vol_adj = (r["volume_raw"] or 0.0) / sf
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
        transitions, final_state = evaluate_segment(seg, card.trigger_level, p)
        for t in transitions:
            conn.execute(
                """
                INSERT INTO signal_facts (symbol, observed_on, signal, state,
                    anchor_id, triggered, active_until, details_json, run_id,
                    rule_version, config_hash, created_at)
                VALUES (?, ?, ?, ?, NULL, 1, NULL, ?, ?, ?, ?, ?)
                """,
                (symbol, t["observed_on"], RIGHT_SIDE_SIGNAL, t["to_state"],
                 json.dumps(t, ensure_ascii=False, sort_keys=True),
                 run_id, RULE_VERSION, config_hash, now),
            )
            res.transitions.append({
                "observed_on": t["observed_on"], "from_state": t["from_state"],
                "to_state": t["to_state"], "reason": t["reason"],
            })
        res.episodes += sum(1 for t in transitions
                            if t["to_state"] == "waiting_retest")
        # 最新版本段的结果为当前状态
        current_state = final_state
        current_level = str(card.trigger_level)

    res.current_state, res.current_level = current_state, current_level

    # ---- 无生效卡/无触发位 → idle + incomplete（§2.5）
    active_card = card_mod.load_active_card(conn, symbol, as_of)
    if active_card is None or active_card.trigger_level is None:
        reason = "no_active_card" if active_card is None else "no_trigger_level"
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
