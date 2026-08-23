"""周线锚点 + 衰竭信号重算入口（D2.1，设计 §5.2、§5.3、§8.2）。

对该股全部完成周逐周重算锚点（weekly_anchors）与五项衰竭信号（signal_facts）：
- 输入 weekly_bars（复权完成周）、daily_bars（复权/不复权日线，锚点周内定位
  与 raw 价）、indicators_weekly（RSI12/MACD 柱，底背离用）；
- signal_facts 本轮 DELETE + 重插，同事务（§2.2 第 3 类、§4.3）；weekly_anchors
  按身份（anchor_type, trade_date, is_fallback）复用旧 anchor_id，身份变化才
  追加新 anchor_id，不覆盖旧行（§5.2）；
- run 记录：pipeline_runs 阶段 weekly_signals，记 config_hash（signals.yaml
  内容哈希）/rule_version（§2.3、§4.2）。

CLI：
    uv run python -m scripts.signals.weekly_signals <symbol> [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from scripts.adapters.common import load_calendar
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals import anchors, exhaustion
from scripts.signals.common import RULE_VERSION, WEEKLY_SIGNALS, WeekBar, load_params


@dataclass
class WeeklySignalsResult:
    symbol: str = ""
    run_id: str = ""
    config_hash: str = ""
    rule_version: str = RULE_VERSION
    weeks: int = 0
    anchors_written: int = 0
    facts_written: int = 0
    current: dict = field(default_factory=dict)       # 最新完成周锚点 + 信号明细
    anchor_history: list[dict] = field(default_factory=list)  # 去重后的锚点序列
    trigger_counts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"{self.symbol} run_id={self.run_id} rule_version={self.rule_version}",
            f"config_hash={self.config_hash[:12]}…",
            f"完成周 {self.weeks}，weekly_anchors {self.anchors_written} 行，"
            f"signal_facts {self.facts_written} 行",
            f"历史触发统计: {self.trigger_counts}",
        ]
        cur = self.current
        if cur:
            pa, ds = cur["panic_anchor"], cur["decline_anchor"]
            lines.append(f"当前周 {cur['week_end_date']}:")
            lines.append(
                f"  恐慌低点锚点: 日期={pa['trade_date']} 复权价={pa['adjusted_price']:.4f} "
                f"不复权价={pa['raw_price']:.4f} fallback={pa['is_fallback']}"
                f" (所在周 {pa['anchor_week']})")
            if ds:
                lines.append(
                    f"  下跌起点锚点: 日期={ds['trade_date']} 复权收盘={ds['adjusted_price']:.4f} "
                    f"不复权收盘={ds['raw_price']:.4f}")
            else:
                lines.append("  下跌起点锚点: 无（恐慌低点周之前无完成周）")
            lines.append(
                f"  活跃信号 {cur['active_count']} 项"
                f"（min_active_signals={cur['min_active']}）: {cur['active_signals']}")
            for s in cur["signals"]:
                lines.append(
                    f"  [{s['signal']}] state={s['state']} triggered={s['triggered']}"
                    f" active_until={s['active_until']} reason={s['reason']}")
        lines.append(f"锚点变更历史 {len(self.anchor_history)} 段（最近 10 段）:")
        for h in self.anchor_history[-10:]:
            lines.append(
                f"  as_of={h['as_of']} {h['anchor_type']}: {h['trade_date']}"
                f" adj={h['adjusted_price']:.4f} raw={h['raw_price']:.4f}"
                f" fallback={h['is_fallback']}")
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


def _load_weeks(conn: sqlite3.Connection, symbol: str) -> list[WeekBar]:
    rows = conn.execute(
        """
        SELECT week_end_date, week_start_date, open_adj, high_adj, low_adj,
               close_adj, volume_adj
        FROM weekly_bars WHERE symbol = ? ORDER BY week_end_date
        """,
        (symbol,),
    ).fetchall()
    if not rows:
        raise ValueError(f"{symbol} 无 weekly_bars，先运行周线聚合")
    return [WeekBar(r["week_end_date"], r["week_start_date"], r["open_adj"],
                    r["high_adj"], r["low_adj"], r["close_adj"], r["volume_adj"])
            for r in rows]


def _load_daily(conn: sqlite3.Connection, symbol: str) -> tuple[dict, str]:
    rows = conn.execute(
        """
        SELECT trade_date, low_raw, close_raw, price_adj_factor, market
        FROM daily_bars WHERE symbol = ? ORDER BY trade_date
        """,
        (symbol,),
    ).fetchall()
    if not rows:
        raise ValueError(f"{symbol} 无 daily_bars，先入库行情")
    daily = {}
    for r in rows:
        f = r["price_adj_factor"] or 1.0
        low_raw = r["low_raw"]
        daily[r["trade_date"]] = {
            # low_raw 缺失保持 None（不当 0 猜，§2.5），锚点定位跳过该日
            "low_adj": low_raw * f if low_raw is not None else None,
            "low_raw": low_raw,
            "close_raw": r["close_raw"],
        }
    return daily, rows[0]["market"]


def _load_indicators(conn: sqlite3.Connection, symbol: str) -> dict:
    rows = conn.execute(
        "SELECT week_end_date, rsi12, macd_hist FROM indicators_weekly WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    return {r["week_end_date"]: (r["rsi12"], r["macd_hist"]) for r in rows}


def _week_end_projector(conn: sqlite3.Connection, market: str,
                        weeks: list[WeekBar]):
    """active_until 投影：目标周在已知完成周内取实际周末日，否则用 trading_calendar
    的 ISO 周最后开市日外推；日历缺失返回 None。"""
    calendar = load_calendar(conn, market)
    by_week: dict[tuple[int, int], list[str]] = {}
    for d, row in calendar.items():
        if row["is_open"]:
            iso = date.fromisoformat(d).isocalendar()
            by_week.setdefault((iso[0], iso[1]), []).append(d)
    keys = sorted(by_week)
    ends = {k: max(v) for k, v in by_week.items()}
    n = len(weeks)

    def project(idx: int) -> str | None:
        if idx < n:
            return weeks[idx].week_end_date
        if not keys:
            return None
        iso = date.fromisoformat(weeks[n - 1].week_end_date).isocalendar()
        try:
            base = keys.index((iso[0], iso[1]))
        except ValueError:
            return None
        target = base + (idx - n + 1)  # 按日历周外推（跳过周不精确，仅展示用）
        return ends[keys[target]] if 0 <= target < len(keys) else None

    return project


def recompute_weekly_signals(
    conn: sqlite3.Connection,
    symbol: str,
    run_id: str | None = None,
    *,
    params: dict | None = None,
    config_hash: str | None = None,
) -> WeeklySignalsResult:
    """全量重算该股周线锚点与衰竭信号（调用方负责事务/提交）。"""
    started_at = utc_now()
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"weekly_signals_{symbol}_{now_compact}"
    if params is None or config_hash is None:
        params, config_hash = load_params()
    res = WeeklySignalsResult(symbol=symbol, run_id=run_id, config_hash=config_hash)

    weeks = _load_weeks(conn, symbol)
    daily, market = _load_daily(conn, symbol)
    indicators = _load_indicators(conn, symbol)
    if len(indicators) < len(weeks):
        res.notes.append(
            f"indicators_weekly 仅 {len(indicators)}/{len(weeks)} 周，"
            f"缺指标周底背离的 RSI/MACD 比较按缺失处理")
    res.weeks = len(weeks)

    steps = anchors.compute_anchor_timeline(weeks, daily, params)

    now = utc_now()
    # 现有锚点建 identity→行 映射：identity = (anchor_type, trade_date, is_fallback)。
    # 跨重算复用旧 anchor_id，不再全删全插（§5.2：身份变化才追加新 anchor_id）。
    existing: dict[tuple, sqlite3.Row] = {}
    for r in conn.execute(
            """
            SELECT anchor_id, as_of, anchor_type, trade_date, adjusted_price,
                   raw_price, is_fallback, run_id
            FROM weekly_anchors WHERE symbol = ?
            """,
            (symbol,)):
        existing[(r["anchor_type"], r["trade_date"], bool(r["is_fallback"]))] = r

    conn.execute(
        f"DELETE FROM signal_facts WHERE symbol = ? AND signal IN "
        f"({', '.join('?' * len(WEEKLY_SIGNALS))})",
        (symbol, *WEEKLY_SIGNALS),
    )

    # 锚点身份已存在 → 复用旧 anchor_id（as_of/价格/run_id 变化则 UPDATE）；
    # 不存在才 INSERT 新 anchor_id；本轮不再产生的旧身份行删除。
    # 相同身份在时间线上必然连续，最近身份直接回填到后续 step。
    round_ids: dict[tuple, int] = {}  # 本轮 identity → anchor_id
    last_keys: dict[str, tuple | None] = {anchors.PANIC_LOW: None,
                                          anchors.DECLINE_START: None}
    for step in steps:
        for anchor, id_attr in (
                (step.panic, "panic_anchor_id"),
                (step.decline, "decline_anchor_id")):
            if anchor is None:
                continue
            key = anchor.key
            if key != last_keys[anchor.anchor_type]:
                last_keys[anchor.anchor_type] = key
                res.anchor_history.append({
                    "as_of": step.as_of, "anchor_type": anchor.anchor_type,
                    "trade_date": anchor.trade_date,
                    "adjusted_price": anchor.adjusted_price,
                    "raw_price": anchor.raw_price,
                    "is_fallback": anchor.is_fallback,
                })
            if key not in round_ids:
                old = existing.get(key)
                if old is not None:
                    anchor_id = old["anchor_id"]
                    if (old["as_of"] != step.as_of
                            or old["adjusted_price"] != anchor.adjusted_price
                            or old["raw_price"] != anchor.raw_price
                            or old["run_id"] != run_id):
                        conn.execute(
                            """
                            UPDATE weekly_anchors SET as_of = ?, adjusted_price = ?,
                                raw_price = ?, run_id = ?, created_at = ?
                            WHERE anchor_id = ?
                            """,
                            (step.as_of, anchor.adjusted_price, anchor.raw_price,
                             run_id, now, anchor_id),
                        )
                        res.anchors_written += 1
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO weekly_anchors (symbol, as_of, anchor_type, trade_date,
                            adjusted_price, raw_price, is_fallback, run_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (symbol, step.as_of, anchor.anchor_type, anchor.trade_date,
                         anchor.adjusted_price, anchor.raw_price,
                         int(anchor.is_fallback), run_id, now),
                    )
                    anchor_id = cur.lastrowid
                    res.anchors_written += 1
                round_ids[key] = anchor_id
            setattr(step, id_attr, round_ids[key])

    stale_ids = [r["anchor_id"] for k, r in existing.items() if k not in round_ids]
    if stale_ids:
        conn.execute(
            f"DELETE FROM weekly_anchors WHERE symbol = ? AND anchor_id IN "
            f"({', '.join('?' * len(stale_ids))})",
            (symbol, *stale_ids),
        )

    project = _week_end_projector(conn, market, weeks)
    rows = exhaustion.compute_signal_rows(
        weeks, indicators, steps, params, active_until_project=project)
    for r in rows:
        conn.execute(
            """
            INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
                triggered, active_until, details_json, run_id, rule_version,
                config_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, r["observed_on"], r["signal"], r["state"], r["anchor_id"],
             r["triggered"], r["active_until"],
             json.dumps(r["details"], ensure_ascii=False, sort_keys=True),
             run_id, RULE_VERSION, config_hash, now),
        )
        res.facts_written += 1

    # 当前周汇总（§5.2 ⚠️：日报必须带出锚点明细供人工核对）
    last = weeks[-1].week_end_date
    step = steps[-1]
    counts = conn.execute(
        f"SELECT signal, SUM(triggered) FROM signal_facts WHERE symbol = ?"
        f" AND signal IN ({', '.join('?' * len(WEEKLY_SIGNALS))}) GROUP BY signal",
        (symbol, *WEEKLY_SIGNALS),
    ).fetchall()
    res.trigger_counts = {r[0]: int(r[1] or 0) for r in counts}
    min_active = params["exhaustion"]["min_active_signals"]
    active = exhaustion.count_active_signals(conn, symbol, last, min_active)
    cur_signals = []
    for r in rows[-len(WEEKLY_SIGNALS):]:
        cur_signals.append({
            "signal": r["signal"], "state": r["state"],
            "triggered": r["triggered"], "active_until": r["active_until"],
            "reason": r["details"].get("reason"),
        })
    res.current = {
        "week_end_date": last,
        "panic_anchor": {
            "trade_date": step.panic.trade_date,
            "adjusted_price": step.panic.adjusted_price,
            "raw_price": step.panic.raw_price,
            "is_fallback": step.panic.is_fallback,
            "anchor_week": weeks[step.panic.week_index].week_end_date,
            "anchor_id": step.panic_anchor_id,
        },
        "decline_anchor": ({
            "trade_date": step.decline.trade_date,
            "adjusted_price": step.decline.adjusted_price,
            "raw_price": step.decline.raw_price,
            "anchor_id": step.decline_anchor_id,
        } if step.decline else None),
        "active_count": active["active_count"],
        "active_signals": active["active_signals"],
        "min_active": min_active,
        "signals": cur_signals,
    }

    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, app_version,
            status, error, started_at, finished_at)
        VALUES (?, 'weekly_signals', ?, ?, NULL, ?, ?, NULL, 'success', NULL, ?, ?)
        """,
        (run_id, utc_now(), last, config_hash, RULE_VERSION, started_at, utc_now()),
    )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.signals.weekly_signals")
    parser.add_argument("symbol")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        with conn:  # DELETE + 重插 + run 记录同一事务（§4.3）
            res = recompute_weekly_signals(conn, args.symbol)
        print(res)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
