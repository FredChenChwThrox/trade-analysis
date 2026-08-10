"""交易日历校验门禁（D1.4，设计 §2.5、§3.5）。

给定 market+date（或 symbol+date）判断：
- trading_with_bars：交易日且该股有行情；
- suspended：交易日但该股无 bar（基准指数有 bar → 个股停牌）；
- source_missing：交易日但个股与指数都无 bar（来源缺数）；
- non_trading_day：日历休市；
- incomplete：日历缺失/超范围/指数与日历冲突，不猜（§2.5）。

指数行情与日历交叉校验：指数有 bar 但日历说休市（或反之）→ 冲突，
输出 incomplete 及原因。指数行情不作为唯一日历来源，只做交叉检查（§3.5）。

CLI：
    uv run python -m scripts.pipeline.calendar_check day 603605.SH 2026-08-07
    uv run python -m scripts.pipeline.calendar_check cross-check CN 2026-07-01 2026-08-09
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from scripts.adapters.common import market_of
from scripts.pipeline.db import DEFAULT_DB_PATH, connect

# 市场默认基准指数（§3.5；watchlist.benchmark_code 可按股票覆盖）
MARKET_DEFAULT_INDEX = {"CN": "000300.SH", "HK": "^HSI"}

STATUS_OK = "trading_with_bars"
STATUS_SUSPENDED = "suspended"
STATUS_SOURCE_MISSING = "source_missing"
STATUS_NON_TRADING = "non_trading_day"
STATUS_INCOMPLETE = "incomplete"


@dataclass
class DayCheck:
    symbol: str
    trade_date: str
    status: str
    reason: str = ""

    def __str__(self) -> str:
        return f"{self.symbol} {self.trade_date}: {self.status}" + (
            f"（{self.reason}）" if self.reason else "")


@dataclass
class CrossCheckResult:
    market: str
    start: str
    end: str
    index_code: str | None = None
    conflicts: list[str] = field(default_factory=list)
    checked_days: int = 0

    @property
    def status(self) -> str:
        return "conflict" if self.conflicts else "ok"

    def __str__(self) -> str:
        head = (f"{self.market} {self.start}~{self.end} index={self.index_code}: "
                f"{self.status}（checked={self.checked_days}）")
        if self.conflicts:
            return head + "\n  " + "\n  ".join(self.conflicts)
        return head


def _calendar_row(conn: sqlite3.Connection, market: str, trade_date: str):
    return conn.execute(
        "SELECT * FROM trading_calendar WHERE market = ? AND trade_date = ?",
        (market, trade_date),
    ).fetchone()


def _calendar_exists(conn: sqlite3.Connection, market: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM trading_calendar WHERE market = ? LIMIT 1", (market,)
    ).fetchone() is not None


def _has_bar(conn: sqlite3.Connection, symbol: str, trade_date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM daily_bars WHERE symbol = ? AND trade_date = ?",
        (symbol, trade_date),
    ).fetchone() is not None


def _has_index_bar(conn: sqlite3.Connection, index_code: str, trade_date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM index_bars WHERE index_code = ? AND trade_date = ?",
        (index_code, trade_date),
    ).fetchone() is not None


def _benchmark_of(conn: sqlite3.Connection, symbol: str, market: str) -> str:
    row = conn.execute(
        "SELECT benchmark_code FROM watchlist WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row and row["benchmark_code"]:
        return row["benchmark_code"]
    return MARKET_DEFAULT_INDEX[market]


def check_symbol_day(conn: sqlite3.Connection, symbol: str, trade_date: str) -> DayCheck:
    """判断某股某日：有行情 / 停牌 / 来源缺数 / 非交易日 / incomplete。"""
    market = market_of(symbol)
    if not _calendar_exists(conn, market):
        return DayCheck(symbol, trade_date, STATUS_INCOMPLETE,
                        f"trading_calendar 缺失（market={market}），无法校验")
    cal = _calendar_row(conn, market, trade_date)
    if cal is None:
        return DayCheck(symbol, trade_date, STATUS_INCOMPLETE,
                        f"{trade_date} 超出 trading_calendar 种子范围（market={market}）")
    if not cal["is_open"]:
        detail = cal["status_detail"] or cal["status"]
        return DayCheck(symbol, trade_date, STATUS_NON_TRADING, f"休市（{detail}）")
    if _has_bar(conn, symbol, trade_date):
        return DayCheck(symbol, trade_date, STATUS_OK)
    benchmark = _benchmark_of(conn, symbol, market)
    if _has_index_bar(conn, benchmark, trade_date):
        return DayCheck(symbol, trade_date, STATUS_SUSPENDED,
                        f"交易日无 bar，基准 {benchmark} 有 bar → 个股停牌")
    return DayCheck(symbol, trade_date, STATUS_SOURCE_MISSING,
                    f"交易日无 bar，基准 {benchmark} 也无 bar → 来源缺数")


def cross_check_index_calendar(
    conn: sqlite3.Connection,
    market: str,
    start: str,
    end: str,
    index_code: str | None = None,
) -> CrossCheckResult:
    """指数行情与日历交叉校验（§3.5）。

    指数有 bar 而日历休市、或日历开市而指数无 bar → 冲突，逐日记入 conflicts。
    日历缺失时返回空结果并记一条 incomplete 说明（不猜）。
    """
    index_code = index_code or MARKET_DEFAULT_INDEX[market]
    res = CrossCheckResult(market=market, start=start, end=end, index_code=index_code)
    if not _calendar_exists(conn, market):
        res.conflicts.append(
            f"incomplete: trading_calendar 缺失（market={market}），无法交叉校验")
        return res
    d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    while d <= end_d:
        ds = d.isoformat()
        cal = _calendar_row(conn, market, ds)
        if cal is None:
            res.conflicts.append(
                f"incomplete: {ds} 超出 trading_calendar 种子范围（market={market}）")
        else:
            res.checked_days += 1
            has_bar = _has_index_bar(conn, index_code, ds)
            if cal["is_open"] and not has_bar:
                res.conflicts.append(
                    f"{ds}: 日历开市但指数 {index_code} 无 bar → 来源缺数或日历错误")
            elif not cal["is_open"] and has_bar:
                res.conflicts.append(
                    f"{ds}: 日历休市（{cal['status']}）但指数 {index_code} 有 bar → 日历错误")
        d += timedelta(days=1)
    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.calendar_check")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_day = sub.add_parser("day", help="判断 symbol 某日：有行情/停牌/缺数/非交易日")
    p_day.add_argument("symbol")
    p_day.add_argument("trade_date")
    p_cross = sub.add_parser("cross-check", help="指数行情与日历交叉校验")
    p_cross.add_argument("market")
    p_cross.add_argument("start")
    p_cross.add_argument("end")
    p_cross.add_argument("--index-code", default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.cmd == "day":
            check = check_symbol_day(conn, args.symbol, args.trade_date)
            print(check)
            return 0 if check.status != STATUS_INCOMPLETE else 2
        res = cross_check_index_calendar(
            conn, args.market, args.start, args.end, args.index_code)
        print(res)
        return 0 if res.status == "ok" else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
