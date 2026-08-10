"""yahoo_finance adapter（设计 §3.2、§3.7）。

覆盖数据类型：
- price         港股日线 CSV → daily_bars
- fx            外汇 CSV → fx_rates（方向在 adapter 内统一为"财务币种→交易币种"）
- stock_actions 分红/拆股 CSV → corporate_actions
- index         指数日线 CSV → index_bars（000300.SS 等 yahoo 代码做别名归一）

已知来源偏差（详见 docs/execution_log.md 2026-08-09 D1.3 条目）：
- Date 列为 UTC 时间戳（如 2026-07-06T16:00:00.000Z），换算到市场本地时区取日期
  （港股 16:00 UTC = 次日 00:00 HKT，即本地交易日 = UTC 日 +1）；
- 无 amount（成交额）列 → amount_raw = NULL；
- OHLC 为长浮点（479.799987793），不复权；Dividends/Stock Splits 列随价格返回，
  本批只入 OHLCV，公司行为以 get_stock_actions 为准；
- 复权因子列本批填 1.0，由 D1.5 复权模块重算。
"""

from __future__ import annotations

import csv
import sqlite3
from decimal import Decimal
from pathlib import Path

from scripts.adapters.common import (
    IngestResult,
    dec_str,
    load_calendar,
    market_of,
    market_tz,
    record_revision,
    utc_now,
    utc_ts_to_local_date,
)
from scripts.adapters.stock_finance_data import (
    _validate_bar_row,
    upsert_daily_bars,
    upsert_index_bars,
)

SOURCE = "yahoo_finance"

# yahoo 指数代码 → 系统内统一指数代码（§3.5 benchmark 口径）
INDEX_CODE_ALIAS = {
    "000300.SS": "000300.SH",
}


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    if s in ("", "NA", "nan"):
        return None
    return float(s)


# ---------------------------------------------------------------- 港股日线 → daily_bars

def parse_price_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """yahoo 日线 CSV → daily_bars。

    列：Date,Open,High,Low,Close,Volume,Dividends,Stock Splits,thscode,...,currency
    """
    calendar_cache: dict[str, dict] = {}
    bars: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            symbol = (rec.get("thscode") or "").strip()
            if not symbol:
                result.conflicts += 1
                result.errors.append("缺少 thscode 列或值为空")
                return result
            market = market_of(symbol)
            trade_date = utc_ts_to_local_date(
                (rec.get("Date") or "").strip(), market_tz(market))
            if market not in calendar_cache:
                calendar_cache[market] = load_calendar(conn, market)
            calendar = calendar_cache[market]
            if not calendar:
                # 该市场日历缺失（如 HK 种子 incomplete_todo）：不猜，记 incomplete（§2.5）
                reason = f"trading_calendar 缺失（market={market}），交易日校验跳过"
                if reason not in result.incomplete_reasons:
                    result.incomplete_reasons.append(reason)
            else:
                cal = calendar.get(trade_date)
                if cal is None:
                    result.conflicts += 1
                    result.errors.append(
                        f"{trade_date} 不在 trading_calendar 种子范围内（market={market}）")
                    return result
                if not cal["is_open"]:
                    result.conflicts += 1
                    result.errors.append(
                        f"{trade_date} 非交易日（{cal['status']}）却有行情 bar（{symbol}）")
                    return result
            row = {
                "open": _num(rec.get("Open")), "high": _num(rec.get("High")),
                "low": _num(rec.get("Low")), "close": _num(rec.get("Close")),
                "volume": _num(rec.get("Volume")), "amount": None,
            }
            bad = _validate_bar_row(row, trade_date)
            if bad == "EMPTY":
                result.skipped += 1
                result.notes.append(f"{trade_date} OHLC 全缺失（残缺 bar），行级跳过")
                continue
            if bad:
                result.conflicts += 1
                result.errors.append(bad)
                return result
            bars.append({
                "symbol": symbol, "trade_date": trade_date, "market": market,
                **row, "currency": (rec.get("currency") or "").strip() or None,
            })
    run_id = Path(path).parent.name
    upsert_daily_bars(conn, bars, source=SOURCE, raw_object_id=raw_object_id,
                      run_id=run_id, result=result)
    return result


# ---------------------------------------------------------------- FX → fx_rates

def parse_fx_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                 result: IngestResult,
                 direction: tuple[str, str] | None = None) -> IngestResult:
    """yahoo FX 日线 CSV → fx_rates。

    汇率对从文件名解析（CNYHKD=X → from=CNY, to=HKD）。换算方向在 adapter 内统一为
    "财务币种→交易币种"（§3.7）：传入 direction=(财务币种, 交易币种) 时，
    若文件方向相反则取倒数入库。
    """
    pair = path.stem.upper().replace("=X", "")
    if len(pair) != 6:
        result.conflicts += 1
        result.errors.append(f"无法从文件名解析汇率对: {path.name}")
        return result
    file_from, file_to = pair[:3], pair[3:]
    invert = False
    if direction is not None:
        want_from, want_to = direction
        if (file_from, file_to) == (want_to, want_from):
            invert = True  # 来源只有反向对 → 取倒数（§3.7）
        elif (file_from, file_to) != (want_from, want_to):
            result.conflicts += 1
            result.errors.append(
                f"汇率对 {file_from}{file_to} 与要求方向 {want_from}->{want_to} 不符")
            return result
    from_ccy, to_ccy = (file_to, file_from) if invert else (file_from, file_to)
    # FX 日期按目标币种市场本地时区换算（CNYHKD 等 HKD 对 → 香港）
    tz = market_tz("HK" if to_ccy == "HKD" or from_ccy == "HKD" else "CN")

    now = utc_now()
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            close = _num(rec.get("Close"))
            if close is None:
                result.skipped += 1
                result.notes.append("FX Close 缺失，行级跳过")
                continue
            if close <= 0:
                result.conflicts += 1
                result.errors.append(f"FX 汇率非正: {close}")
                return result
            rate_date = utc_ts_to_local_date((rec.get("Date") or "").strip(), tz)
            rate = Decimal(str(close))
            if invert:
                rate = Decimal(1) / rate
            rate_s = format(rate.normalize(), "f")
            old = conn.execute(
                "SELECT rate FROM fx_rates WHERE from_currency=? AND to_currency=? AND rate_date=?",
                (from_ccy, to_ccy, rate_date),
            ).fetchone()
            if old is None:
                conn.execute(
                    """
                    INSERT INTO fx_rates (from_currency, to_currency, rate_date, rate,
                                          source, available_at, raw_object_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (from_ccy, to_ccy, rate_date, rate_s, SOURCE, now, raw_object_id, now),
                )
                result.inserted += 1
            elif old["rate"] == rate_s:
                result.skipped += 1
            else:
                conn.execute(
                    """
                    UPDATE fx_rates SET rate=?, source=?, raw_object_id=?, updated_at=?
                    WHERE from_currency=? AND to_currency=? AND rate_date=?
                    """,
                    (rate_s, SOURCE, raw_object_id, now, from_ccy, to_ccy, rate_date),
                )
                record_revision(
                    conn, table_name="fx_rates",
                    record_key={"from_currency": from_ccy, "to_currency": to_ccy,
                                "rate_date": rate_date},
                    old_value={"rate": old["rate"]}, new_value={"rate": rate_s},
                    source=SOURCE, reason="source_revision",
                    run_id=Path(path).parent.name,
                )
                result.updated += 1
    return result


# ---------------------------------------------------------------- stock_actions → corporate_actions

def parse_stock_actions_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                            result: IngestResult) -> IngestResult:
    """yahoo get_stock_actions CSV → corporate_actions。

    列：Date,Dividends,Stock Splits。Dividends>0 → cash_dividend；
    Stock Splits>0 → split。UNIQUE(symbol, ex_date, action_type) 冲突即跳过（幂等）。
    """
    symbol = path.stem
    market = market_of(symbol)
    tz = market_tz(market)
    now = utc_now()
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            ex_date = utc_ts_to_local_date((rec.get("Date") or "").strip(), tz)
            dividend = _num(rec.get("Dividends")) or 0.0
            split = _num(rec.get("Stock Splits")) or 0.0
            rows: list[tuple[str, str | None, str | None]] = []
            if dividend > 0:
                rows.append(("cash_dividend", dec_str(dividend), None))
            if split > 0:
                rows.append(("split", None, dec_str(split)))
            if not rows:
                result.skipped += 1
                continue
            for action_type, cash_per_share, split_ratio in rows:
                exists = conn.execute(
                    "SELECT 1 FROM corporate_actions WHERE symbol=? AND ex_date=? AND action_type=?",
                    (symbol, ex_date, action_type),
                ).fetchone()
                if exists:
                    result.skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO corporate_actions (symbol, ex_date, action_type,
                        cash_per_share, split_ratio, details_json,
                        source, available_at, raw_object_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (symbol, ex_date, action_type, cash_per_share, split_ratio,
                     '{"sources": ["yahoo_finance.get_stock_actions"]}',
                     SOURCE, raw_object_id, now),
                )
                result.inserted += 1
    return result


# ---------------------------------------------------------------- 指数 → index_bars

def parse_index_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """yahoo 指数日线 CSV → index_bars（代码经 INDEX_CODE_ALIAS 归一）。"""
    bars: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            code = (rec.get("thscode") or "").strip()
            code = INDEX_CODE_ALIAS.get(code, code)
            market = market_of(code)
            trade_date = utc_ts_to_local_date(
                (rec.get("Date") or "").strip(), market_tz(market))
            row = {
                "open": _num(rec.get("Open")), "high": _num(rec.get("High")),
                "low": _num(rec.get("Low")), "close": _num(rec.get("Close")),
                "volume": _num(rec.get("Volume")),
            }
            bad = _validate_bar_row(row, trade_date)
            if bad == "EMPTY":
                result.skipped += 1
                result.notes.append(f"{trade_date} OHLC 全缺失（残缺 bar），行级跳过")
                continue
            if bad:
                result.conflicts += 1
                result.errors.append(f"{code} {bad}")
                return result
            bars.append({
                "index_code": code, "trade_date": trade_date, **row,
                "currency": (rec.get("currency") or "").strip() or None,
            })
    upsert_index_bars(conn, bars, source=SOURCE, raw_object_id=raw_object_id,
                      run_id=Path(path).parent.name, result=result)
    return result
