"""API 查询参数解析与校验（docs/ui_design_phase1.md §4 通用约定）。

所有解析函数在参数非法时抛 ValueError，由路由层转 400。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PRICE_MODES = {"unadjusted", "fully_adjusted", "adjusted_back"}
GRANULARITIES = {"daily", "weekly"}


class ParseError(ValueError):
    pass


def int_arg(args, name: str, default: int) -> int:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"参数 {name} 必须是整数，收到: {raw!r}") from exc


def bool_arg(args, name: str, default: bool | None = None) -> bool | None:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    if raw in ("1", "true", "True", "yes"):
        return True
    if raw in ("0", "false", "False", "no"):
        return False
    raise ParseError(f"参数 {name} 必须是 0/1，收到: {raw!r}")


def _validate_date(s: str) -> str:
    if not _DATE_RE.match(s):
        raise ParseError(f"日期格式必须是 YYYY-MM-DD，收到: {s!r}")
    return s


def date_range(args, default_days: int = 90, required: bool = False) -> tuple[str | None, str | None]:
    start, end = args.get("start"), args.get("end")
    if required and not (start and end):
        raise ParseError("参数 start/end 必填")
    if start:
        start = _validate_date(start)
    if end:
        end = _validate_date(end)
    if not start and not end and not required:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=default_days)).isoformat()
    if start and end and start > end:
        raise ParseError(f"start({start}) 不能晚于 end({end})")
    return start, end


def price_mode(args, default: str = "unadjusted") -> str:
    raw = args.get("price", default)
    if raw not in PRICE_MODES:
        raise ParseError(f"price 必须是 {'/'.join(sorted(PRICE_MODES))}，收到: {raw!r}")
    return raw


def granularity(args, default: str = "daily") -> str:
    raw = args.get("granularity", default)
    if raw not in GRANULARITIES:
        raise ParseError(f"granularity 必须是 daily/weekly，收到: {raw!r}")
    return raw


def fields_arg(args, max_fields: int | None = None, required: bool = False) -> list[str]:
    raw = args.get("fields")
    if not raw:
        if required:
            raise ParseError("参数 fields 必填")
        return []
    fields = [f.strip() for f in raw.split(",") if f.strip()]
    if max_fields and len(fields) > max_fields:
        raise ParseError(f"fields 最多 {max_fields} 个，收到 {len(fields)} 个")
    return fields


def symbol_list_arg(args, name: str, max_n: int, required: bool = False) -> list[str]:
    raw = args.get(name)
    if not raw:
        if required:
            raise ParseError(f"参数 {name} 必填")
        return []
    symbols = [s.strip() for s in raw.split(",") if s.strip()]
    if len(symbols) > max_n:
        raise ParseError(f"{name} 最多 {max_n} 个，收到 {len(symbols)} 个")
    if len(symbols) < 2 and name == "symbols" and max_n >= 2:
        # compare 至少 2 只在路由层按场景校验；此处只做通用多选拆分
        pass
    return symbols


def sort_arg(args, name: str, default: str, allowed: set[str]) -> str:
    raw = args.get(name, default)
    if raw not in allowed:
        raise ParseError(f"sort 必须是 {'/'.join(sorted(allowed))} 之一，收到: {raw!r}")
    return raw


def order_arg(args, default: str = "desc") -> str:
    raw = args.get("order", default).lower()
    if raw not in ("asc", "desc"):
        raise ParseError(f"order 必须是 asc/desc，收到: {raw!r}")
    return raw


def _list_arg(args, name: str) -> list[str]:
    vals = args.getlist(name) if hasattr(args, "getlist") else args.get(name, "").split(",")
    return [v.strip() for v in vals if v.strip()]


def stock_filters(args) -> dict:
    """/api/stocks 筛选条件。"""
    filters: dict = {}
    market = _list_arg(args, "market")
    if market:
        filters["market"] = market
    q = (args.get("q") or "").strip()
    if q:
        filters["q"] = q
    hc = bool_arg(args, "has_active_card")
    if hc is not None:
        filters["has_active_card"] = hc
    for key in ("pe_min", "pe_max", "pct_chg_min", "pct_chg_max", "volume_min", "volume_max"):
        raw = args.get(key)
        if raw is not None and raw != "":
            filters[key] = float(raw)
    pe_status = _list_arg(args, "pe_status")
    if pe_status:
        filters["pe_status"] = pe_status
    rsl = args.get("recent_signal_days")
    if rsl:
        filters["recent_signal_days"] = int(rsl)
    return filters


def signal_filters(args) -> dict:
    filters: dict = {}
    symbols = _list_arg(args, "symbols")
    if symbols:
        filters["symbols"] = symbols
    signals = _list_arg(args, "signals")
    if signals:
        filters["signals"] = signals
    states = _list_arg(args, "states")
    if states:
        filters["states"] = states
    trig = bool_arg(args, "triggered")
    if trig is not None:
        filters["triggered"] = trig
    start, end = date_range(args, required=False)
    if start:
        filters["start"] = start
    if end:
        filters["end"] = end
    if args.get("anchor_id"):
        filters["anchor_id"] = int(args["anchor_id"])
    return filters


def card_filters(args) -> dict:
    filters: dict = {}
    if args.get("symbol"):
        filters["symbol"] = args["symbol"]
    status = _list_arg(args, "status")
    if status:
        filters["status"] = status
    if args.get("effective_from"):
        filters["effective_from"] = _validate_date(args["effective_from"])
    if args.get("effective_to"):
        filters["effective_to"] = _validate_date(args["effective_to"])
    return filters


def run_filters(args) -> dict:
    filters: dict = {}
    for key in ("run_id", "stage"):
        if args.get(key):
            filters[key] = args[key]
    status = _list_arg(args, "status")
    if status:
        filters["status"] = status
    if args.get("start"):
        filters["start"] = _validate_date(args["start"])
    if args.get("end"):
        filters["end"] = _validate_date(args["end"])
    return filters


def report_filters(args) -> dict:
    filters: dict = {}
    for key in ("report_type", "symbol", "trade_date", "status", "report_run_id"):
        if args.get(key):
            filters[key] = args[key]
    return filters
