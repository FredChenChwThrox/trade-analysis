"""周线聚合（D1.6，设计 §3.4、§4.1）。

口径：
- 逐日复权后聚合：每个交易日用自己的 price_adj_factor（开=周首日、收=周末日、
  高低=周内逐日复权极值），成交量 = Σ volume_raw / share_factor，
  成交额 = Σ amount_raw（不做股份因子调整，§4.1）。
  不得先聚合不复权 OHLC 再乘周末单一因子——周中除权会扭曲周高/周低/K 线形态。
- 只写完成周：周按 ISO 周（周一至周日）划分，该周最后一个交易日由
  trading_calendar 判定（不写死星期五）；该日已过（<= 数据截止）且有 bar 才写。
  进行时周不写入。
- weekly_bars 为派生数据：整 symbol 删除后重算（§2.2 第 3 类），调用方负责事务。

CLI：
    uv run python -m scripts.pipeline.weekly <symbol> [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from scripts.adapters.common import load_calendar
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now


@dataclass
class WeeklyResult:
    symbol: str = ""
    weeks_written: int = 0
    last_week_end: str | None = None
    skipped_in_progress: list[str] = field(default_factory=list)  # 进行时周的周末日
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = (f"{self.symbol}: weeks={self.weeks_written} "
             f"last_week_end={self.last_week_end}")
        if self.skipped_in_progress:
            s += f" 进行时周跳过={self.skipped_in_progress}"
        if self.notes:
            s += "\n  " + "\n  ".join(self.notes)
        return s


def _iso_week(d: str) -> tuple[int, int]:
    iso = date.fromisoformat(d).isocalendar()
    return (iso[0], iso[1])


def _factor(b: sqlite3.Row, col: str) -> float:
    v = b[col]
    return float(v) if v else 1.0  # None/0 兜底为 1.0（因子缺失不应发生，缺失时宁可不缩放）


def rebuild_weekly(
    conn: sqlite3.Connection,
    symbol: str,
    run_id: str | None = None,
) -> WeeklyResult:
    """按当前 daily_bars 因子全量重建该股周线（不提交事务）。

    完成周判定：ISO 周最后开市日（trading_calendar）<= 该股最新 bar 日期，
    且该日有 bar。进行时周 / 周末日无 bar（停牌）的周跳过并记 note。
    """
    res = WeeklyResult(symbol=symbol)
    bars = conn.execute(
        "SELECT * FROM daily_bars WHERE symbol = ? ORDER BY trade_date", (symbol,),
    ).fetchall()
    if not bars:
        raise ValueError(f"{symbol} 无 daily_bars，先入库行情")
    market = bars[0]["market"]
    calendar = load_calendar(conn, market)
    if not calendar:
        res.notes.append(
            f"trading_calendar 缺失（market={market}），周线 incomplete，未写入（§2.5）")
        return res

    open_by_week: dict[tuple[int, int], list[str]] = {}
    for d, row in calendar.items():
        if row["is_open"]:
            open_by_week.setdefault(_iso_week(d), []).append(d)
    for days in open_by_week.values():
        days.sort()

    bars_by_week: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for b in bars:
        bars_by_week.setdefault(_iso_week(b["trade_date"]), []).append(b)

    max_bar = bars[-1]["trade_date"]
    rows: list[tuple] = []
    for key in sorted(bars_by_week):
        open_days = open_by_week.get(key)
        if not open_days:
            res.notes.append(f"ISO 周 {key} 在 trading_calendar 中无开市日"
                             f"（日历范围不足？），跳过")
            continue
        week_end = open_days[-1]
        if week_end > max_bar:
            res.skipped_in_progress.append(week_end)  # 进行时周不写入
            continue
        week_bars = bars_by_week[key]
        if week_end not in {b["trade_date"] for b in week_bars}:
            res.notes.append(
                f"周 {open_days[0]}~{week_end}：周末交易日无 bar（停牌？），跳过")
            continue
        open_adj = week_bars[0]["open_raw"] * _factor(week_bars[0], "price_adj_factor")
        close_adj = week_bars[-1]["close_raw"] * _factor(week_bars[-1], "price_adj_factor")
        high_adj = max(b["high_raw"] * _factor(b, "price_adj_factor") for b in week_bars)
        low_adj = min(b["low_raw"] * _factor(b, "price_adj_factor") for b in week_bars)
        volume_adj = sum(
            (b["volume_raw"] or 0.0) / _factor(b, "share_factor") for b in week_bars)
        amounts = [b["amount_raw"] for b in week_bars]
        amount = None if all(a is None for a in amounts) else sum(
            a or 0.0 for a in amounts)
        rows.append((
            symbol, week_end, open_days[0],
            open_adj, high_adj, low_adj, close_adj,
            volume_adj, amount, len(week_bars), run_id,
        ))

    conn.execute("DELETE FROM weekly_bars WHERE symbol = ?", (symbol,))
    conn.executemany(
        """
        INSERT INTO weekly_bars (symbol, week_end_date, week_start_date,
            open_adj, high_adj, low_adj, close_adj,
            volume_adj, amount_raw, trading_days, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    res.weeks_written = len(rows)
    res.last_week_end = rows[-1][1] if rows else None
    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.weekly")
    parser.add_argument("symbol")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        started_at = utc_now()
        run_id = f"weekly_{args.symbol}_{started_at[:19]}"
        with conn:
            res = rebuild_weekly(conn, args.symbol, run_id=run_id)
            status = "success" if res.weeks_written else "degraded"
            conn.execute(
                """
                INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of,
                    status, started_at, finished_at)
                VALUES (?, 'weekly', ?, ?, ?, ?)
                """,
                (run_id, utc_now(), status, started_at, utc_now()),
            )
        print(res)
        return 0 if res.weeks_written else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
