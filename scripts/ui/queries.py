"""UI 数据查询层：封装全部只读 SQL（docs/ui_design_phase1.md §4，task-01）。

约定：
- 所有查询函数第一个参数为 sqlite3.Connection（由 scripts/ui/db.get_connection 提供）。
- 全部只读，不修改数据库；参数化 SQL，禁止字符串拼接表名/字段名（排序字段白名单）。
- 价格口径在应用层计算，不改动指标存储口径（§5.1/§5.4）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

# 价格刻度类指标：与价格轴同量纲，不复权展示时需要 ÷ 当日因子折回（§5.1）
PRICE_SCALE_FIELDS = {
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
    "boll_mid", "boll_upper", "boll_lower",
}

_INDICATOR_TABLES = {
    "daily": ("indicators_daily", "trade_date"),
    "weekly": ("indicators_weekly", "week_end_date"),
}

_CARD_PARSE_FIELDS = [
    "earnings_scenarios_json", "valuation_scenarios_json", "price_tiers_json",
    "invalidation_json", "swing_box_json", "right_side_trigger_json", "input_snapshot_json",
]


def _fetch_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


# ---------------------------------------------------------------- 基础工具

def list_markets(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT market FROM watchlist ORDER BY market").fetchall()
    return [r[0] for r in rows]


def list_run_ids(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return _fetch_dicts(
        conn,
        """
        SELECT run_id, MAX(started_at) AS last_run_at
        FROM pipeline_runs GROUP BY run_id ORDER BY last_run_at DESC LIMIT ?
        """,
        (limit,),
    )


def list_card_status(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT status FROM strategy_card_versions ORDER BY status"
    ).fetchall()
    return [r[0] for r in rows]


def get_watchlist(conn: sqlite3.Connection) -> list[dict]:
    return _fetch_dicts(conn, "SELECT * FROM watchlist ORDER BY symbol")


def search_stocks(conn: sqlite3.Connection, q: str, limit: int = 10) -> list[dict]:
    if not q:
        return _fetch_dicts(
            conn, "SELECT symbol, market, name FROM watchlist ORDER BY symbol LIMIT ?", (limit,)
        )
    like = f"%{q}%"
    return _fetch_dicts(
        conn,
        """
        SELECT symbol, market, name FROM watchlist
        WHERE symbol LIKE ? OR name LIKE ? OR aliases_json LIKE ?
        ORDER BY symbol LIMIT ?
        """,
        (like, like, like, limit),
    )


# ---------------------------------------------------------------- 股票列表

def _signal_count_sql(n: int) -> str:
    """最近 n 个交易日内有 triggered 信号的计数标量子查询。"""
    return (
        f"(SELECT COUNT(*) FROM signal_facts sf "
        f" WHERE sf.symbol = w.symbol AND sf.triggered = 1 "
        f"   AND sf.observed_on >= (SELECT MIN(trade_date) FROM "
        f"         (SELECT trade_date FROM daily_bars dd "
        f"          WHERE dd.symbol = w.symbol ORDER BY trade_date DESC LIMIT {n})))"
    )


def compute_tier_state(close: float | None, card: dict | None) -> dict | None:
    """根据现价（不复权）与 active 卡三档价区计算档位位置。

    返回 {tier, zone_low, zone_high, dist_pct}；未进入任何档时返回
    {tier: None, zone_low, zone_high, dist_pct}（距最近档边界百分比）。
    """
    if close is None or not card or not card.get("price_tiers_json"):
        return None
    tiers = card["price_tiers_json"].get("tiers", [])
    if not tiers:
        return None
    for t in tiers:
        low, high = float(t["zone_low"]), float(t["zone_high"])
        if low <= close <= high:
            dist = min((close - low) / low, (high - close) / high) * 100
            return {"tier": t["tier"], "zone_low": t["zone_low"], "zone_high": t["zone_high"],
                    "dist_pct": round(dist, 3)}
    # 未进入任何档：找最近档边界
    candidates = []
    for t in tiers:
        candidates.append((abs(close - float(t["zone_low"])), t, float(t["zone_low"])))
        candidates.append((abs(close - float(t["zone_high"])), t, float(t["zone_high"])))
    _, t, bound = min(candidates)
    dist = (close - bound) / bound * 100
    return {"tier": None, "zone_low": t["zone_low"], "zone_high": t["zone_high"],
            "dist_pct": round(dist, 3), "nearest": bound}


def _list_stocks_base_sql(recent_signal_days: int) -> str:
    return f"""
    SELECT w.symbol, w.market, w.name, w.benchmark_code, w.currency, w.timezone,
           l.trade_date AS latest_trade_date,
           b.close_raw AS latest_close, b.volume_raw AS latest_volume,
           b.price_adj_factor, b.share_factor, b.trading_status,
           i.pe_ttm, i.pe_status, i.pct_chg,
           (SELECT card_version_id FROM strategy_card_versions c
             WHERE c.symbol = w.symbol AND c.status = 'active' LIMIT 1) AS active_card_id,
           (SELECT price_tiers_json FROM strategy_card_versions c
             WHERE c.symbol = w.symbol AND c.status = 'active' LIMIT 1) AS _active_tiers,
           {_signal_count_sql(recent_signal_days)} AS signal_count_5d,
           (SELECT pr.status FROM pipeline_runs pr
             WHERE pr.stage = 'symbol:' || w.symbol
             ORDER BY pr.started_at DESC LIMIT 1) AS last_run_status,
           (SELECT pr.error FROM pipeline_runs pr
             WHERE pr.stage = 'symbol:' || w.symbol
             ORDER BY pr.started_at DESC LIMIT 1) AS last_run_error
    FROM watchlist w
    LEFT JOIN (SELECT symbol, MAX(trade_date) AS trade_date FROM daily_bars GROUP BY symbol) l
        ON l.symbol = w.symbol
    LEFT JOIN daily_bars b ON b.symbol = l.symbol AND b.trade_date = l.trade_date
    LEFT JOIN indicators_daily i ON i.symbol = l.symbol AND i.trade_date = l.trade_date
    """


def _list_stocks_where(filters: dict) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    if filters.get("market"):
        markets = _as_list(filters["market"])
        clauses.append(f"w.market IN ({','.join('?' * len(markets))})")
        params.extend(markets)
    if filters.get("q"):
        like = f"%{filters['q']}%"
        clauses.append("(w.symbol LIKE ? OR w.name LIKE ? OR w.aliases_json LIKE ?)")
        params.extend([like, like, like])
    hc = filters.get("has_active_card")
    if hc is True:
        clauses.append("EXISTS (SELECT 1 FROM strategy_card_versions c "
                       "WHERE c.symbol = w.symbol AND c.status = 'active')")
    elif hc is False:
        clauses.append("NOT EXISTS (SELECT 1 FROM strategy_card_versions c "
                       "WHERE c.symbol = w.symbol AND c.status = 'active')")
    for key, op in [("pe_min", ">="), ("pe_max", "<=")]:
        if filters.get(key) is not None:
            clauses.append(f"i.pe_ttm {op} ?")
            params.append(filters[key])
    for key, col, op in [("pct_chg_min", "i.pct_chg", ">="), ("pct_chg_max", "i.pct_chg", "<=")]:
        if filters.get(key) is not None:
            clauses.append(f"{col} {op} ?")
            params.append(filters[key])
    for key, col, op in [("volume_min", "b.volume_raw", ">="), ("volume_max", "b.volume_raw", "<=")]:
        if filters.get(key) is not None:
            clauses.append(f"{col} {op} ?")
            params.append(filters[key])
    if filters.get("pe_status"):
        pe_clauses: list[str] = []
        for s in _as_list(filters["pe_status"]):
            if s == "ok":
                pe_clauses.append("(i.pe_status = '' OR i.pe_status LIKE 'ok%')")
            elif s == "degraded":
                pe_clauses.append("i.pe_status LIKE '%degraded%'")
            elif s == "missing":
                pe_clauses.append("(i.pe_status IS NOT NULL AND i.pe_status != '' "
                                  "AND i.pe_status NOT LIKE 'ok%' AND i.pe_status NOT LIKE '%degraded%')")
            else:
                pe_clauses.append("i.pe_status = ?")
                params.append(s)
        if pe_clauses:
            clauses.append("(" + " OR ".join(pe_clauses) + ")")
    if filters.get("recent_signal_days"):
        n = int(filters["recent_signal_days"])
        clauses.append(f"({_signal_count_sql(n)}) > 0")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


_SORT_COLUMNS = {
    "latest_trade_date": "latest_trade_date",
    "latest_close": "latest_close",
    "pe_ttm": "pe_ttm",
    "pct_chg": "pct_chg",
    "signal_count_5d": "signal_count_5d",
}


def list_stocks(conn: sqlite3.Connection, filters: dict | None = None,
                page: int = 1, page_size: int = 50,
                sort: str = "latest_trade_date", order: str = "desc") -> dict:
    filters = filters or {}
    recent = int(filters.get("recent_signal_days") or 5)
    base = _list_stocks_base_sql(recent)
    where, params = _list_stocks_where(filters)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM ({base}{where}) t", params
    ).fetchone()["n"]

    sort_col = _SORT_COLUMNS.get(sort, "latest_trade_date")
    asc = "ASC" if order == "asc" else "DESC"
    nulls = "NULLS LAST" if asc == "DESC" else "NULLS FIRST"
    offset = (page - 1) * page_size
    rows = _fetch_dicts(
        conn,
        f"{base}{where} ORDER BY {sort_col} {asc} {nulls}, w.symbol ASC "
        f"LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    for r in rows:
        card = {"price_tiers_json": _safe_json(r["_active_tiers"])} if r["_active_tiers"] else None
        r["tier_state"] = compute_tier_state(r["latest_close"], card)
        del r["_active_tiers"]
    return {"page": page, "page_size": page_size, "total": total, "items": rows}


# ---------------------------------------------------------------- 单股基础

def get_stock_meta(conn: sqlite3.Connection, symbol: str) -> dict | None:
    w = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    if w is None:
        return None
    wd = dict(w)
    try:
        wd["aliases"] = json.loads(wd.get("aliases_json") or "[]")
    except json.JSONDecodeError:
        wd["aliases"] = []
    del wd["aliases_json"]

    latest = conn.execute(
        "SELECT trade_date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
        "       amount_raw, price_adj_factor, share_factor, trading_status "
        "FROM daily_bars WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1", (symbol,)
    ).fetchone()
    latest = dict(latest) if latest else None

    ind = None
    if latest:
        ind = conn.execute(
            "SELECT ma5, ma20, ma60, ma120, ma250, dif, dea, macd_hist, rsi6, rsi12, rsi24, "
            "       boll_mid, boll_upper, boll_lower, kdj_k, kdj_d, kdj_j, "
            "       vol_ma5, vol_ma10, pct_chg, amplitude, pe_ttm, pe_status "
            "FROM indicators_daily WHERE symbol = ? AND trade_date = ?",
            (symbol, latest["trade_date"]),
        ).fetchone()
        ind = dict(ind) if ind else None

    card_row = conn.execute(
        "SELECT * FROM strategy_card_versions WHERE symbol = ? AND status = 'active' LIMIT 1",
        (symbol,),
    ).fetchone()
    active_card = _parse_card(dict(card_row)) if card_row else None
    tier_state = compute_tier_state(
        latest["close_raw"] if latest else None, active_card)

    recent_runs = _fetch_dicts(
        conn,
        """
        SELECT run_id, stage, status, as_of, data_cutoff, started_at, finished_at,
               adapter_version, config_hash, rule_version, card_version_id, error
        FROM pipeline_runs
        WHERE stage LIKE ? ORDER BY started_at DESC LIMIT 20
        """,
        (f"%{symbol}%",),
    )
    return {
        "symbol": symbol, "market": wd["market"], "name": wd["name"],
        "currency": wd["currency"], "timezone": wd["timezone"],
        "benchmark_code": wd["benchmark_code"], "active": wd["active"],
        "aliases": wd["aliases"],
        "latest": latest,
        "indicators": ind,
        "active_card": active_card,
        "tier_state": tier_state,
        "recent_runs": recent_runs,
    }


def get_stock_bars(conn: sqlite3.Connection, symbol: str, granularity: str = "daily",
                   start: str | None = None, end: str | None = None,
                   price: str = "unadjusted") -> list[dict]:
    if granularity == "daily":
        return _get_daily_bars(conn, symbol, start, end, price)
    if granularity == "weekly":
        return _get_weekly_bars(conn, symbol, start, end, price)
    raise ValueError(f"unsupported granularity: {granularity}")


def _date_where(date_col: str, start: str | None, end: str | None) -> tuple[str, list]:
    clauses, params = [], []
    if start:
        clauses.append(f"{date_col} >= ?")
        params.append(start)
    if end:
        clauses.append(f"{date_col} <= ?")
        params.append(end)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _get_daily_bars(conn: sqlite3.Connection, symbol: str,
                    start: str | None, end: str | None, price: str) -> list[dict]:
    where, params = _date_where("trade_date", start, end)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT trade_date, open_raw, high_raw, low_raw, close_raw, volume_raw,
               amount_raw, price_adj_factor, share_factor, trading_status
        FROM daily_bars WHERE symbol = ? {where} ORDER BY trade_date ASC
        """,
        [symbol] + params,
    )
    out = []
    for r in rows:
        f, s = r["price_adj_factor"] or 1.0, r["share_factor"] or 1.0
        if price == "fully_adjusted":
            item = {
                "trade_date": r["trade_date"],
                "open": (r["open_raw"] or 0) * f,
                "high": (r["high_raw"] or 0) * f,
                "low": (r["low_raw"] or 0) * f,
                "close": (r["close_raw"] or 0) * f,
                "volume": (r["volume_raw"] or 0) / s,
                "amount": r["amount_raw"],
                "price_adj_factor": r["price_adj_factor"],
            }
        else:  # unadjusted / adjusted_back
            item = {
                "trade_date": r["trade_date"],
                "open": r["open_raw"], "high": r["high_raw"], "low": r["low_raw"],
                "close": r["close_raw"], "volume": r["volume_raw"],
                "amount": r["amount_raw"], "price_adj_factor": r["price_adj_factor"],
            }
        out.append(item)
    return out


def _get_weekly_bars(conn: sqlite3.Connection, symbol: str,
                     start: str | None, end: str | None, price: str) -> list[dict]:
    where, params = _date_where("week_end_date", start, end)
    weeks = _fetch_dicts(
        conn,
        f"SELECT week_start_date, week_end_date FROM weekly_bars "
        f"WHERE symbol = ? {where} ORDER BY week_end_date ASC",
        [symbol] + params,
    )
    if price == "fully_adjusted":
        rows = _fetch_dicts(
            conn,
            f"""
            SELECT week_end_date, open_adj, high_adj, low_adj, close_adj, volume_adj
            FROM weekly_bars WHERE symbol = ? {where} ORDER BY week_end_date ASC
            """,
            [symbol] + params,
        )
        return [{
            "trade_date": r["week_end_date"], "open": r["open_adj"], "high": r["high_adj"],
            "low": r["low_adj"], "close": r["close_adj"], "volume": r["volume_adj"],
        } for r in rows]

    # 不复权：按周边界对 daily_bars 原始值聚合（等价 pipeline/weekly.py 的不复权口径）
    if not weeks:
        return []
    day_start = weeks[0]["week_start_date"]
    day_end = weeks[-1]["week_end_date"]
    days = _fetch_dicts(
        conn,
        """
        SELECT trade_date, open_raw, high_raw, low_raw, close_raw, volume_raw
        FROM daily_bars WHERE symbol = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (symbol, day_start, day_end),
    )
    buckets: dict[str, list[dict]] = {w["week_end_date"]: [] for w in weeks}
    for d in days:
        for w in weeks:
            if w["week_start_date"] <= d["trade_date"] <= w["week_end_date"]:
                buckets[w["week_end_date"]].append(d)
                break
    out = []
    for w in weeks:
        grp = buckets[w["week_end_date"]]
        if not grp:
            continue
        out.append({
            "trade_date": w["week_end_date"],
            "open": grp[0]["open_raw"],
            "high": max(x["high_raw"] for x in grp),
            "low": min(x["low_raw"] for x in grp),
            "close": grp[-1]["close_raw"],
            "volume": sum(x["volume_raw"] or 0 for x in grp),
        })
    return out


def _indicator_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    skip = {"symbol", "trade_date", "week_end_date", "run_id", "rule_version",
            "config_hash", "computed_at"}
    return [r["name"] for r in rows if r["name"] not in skip]


def get_stock_indicators(conn: sqlite3.Connection, symbol: str, granularity: str = "daily",
                         start: str | None = None, end: str | None = None,
                         fields: list[str] | None = None,
                         price: str = "unadjusted") -> list[dict]:
    if granularity not in _INDICATOR_TABLES:
        raise ValueError(f"unsupported granularity: {granularity}")
    table, date_col = _INDICATOR_TABLES[granularity]
    allowed = _indicator_columns(conn, table)
    if fields is None:
        select = allowed
    else:
        bad = [f for f in fields if f not in allowed]
        if bad:
            raise ValueError(f"unknown indicator fields: {bad}")
        select = list(dict.fromkeys(fields))

    where, params = _date_where(date_col, start, end)
    cols = ", ".join(select)
    rows = _fetch_dicts(
        conn,
        f"SELECT {date_col} AS date, {cols} FROM {table} "
        f"WHERE symbol = ? {where} ORDER BY {date_col} ASC",
        [symbol] + params,
    )

    # 不复权 / 折回：价格刻度指标 ÷ 当日因子（§5.1）
    if price in ("unadjusted", "adjusted_back"):
        factors = {}
        date_rng = _fetch_dicts(
            conn,
            f"SELECT trade_date, price_adj_factor FROM daily_bars "
            f"WHERE symbol = ? AND trade_date >= ? AND trade_date <= ?",
            (symbol, start or "0000-01-01", end or "9999-12-31"),
        )
        factors = {r["trade_date"]: r["price_adj_factor"] or 1.0 for r in date_rng}
        scale = [c for c in select if c in PRICE_SCALE_FIELDS]
        for r in rows:
            f = factors.get(r["date"], 1.0)
            for c in scale:
                if r[c] is not None:
                    r[c] = r[c] / f
    return rows


# ---------------------------------------------------------------- 信号

def list_signals(conn: sqlite3.Connection, filters: dict | None = None,
                 page: int = 1, page_size: int = 100,
                 sort: str = "observed_on", order: str = "desc") -> dict:
    filters = filters or {}
    clauses, params = [], []

    if filters.get("symbols"):
        symbols = _as_list(filters["symbols"])
        clauses.append(f"symbol IN ({','.join('?' * len(symbols))})")
        params.extend(symbols)
    if filters.get("signals"):
        signals = _as_list(filters["signals"])
        clauses.append(f"signal IN ({','.join('?' * len(signals))})")
        params.extend(signals)
    if filters.get("states"):
        states = _as_list(filters["states"])
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    if filters.get("triggered") is not None:
        clauses.append("triggered = ?")
        params.append(1 if filters["triggered"] else 0)
    if filters.get("start"):
        clauses.append("observed_on >= ?")
        params.append(filters["start"])
    if filters.get("end"):
        clauses.append("observed_on <= ?")
        params.append(filters["end"])
    if filters.get("anchor_id"):
        clauses.append("anchor_id = ?")
        params.append(filters["anchor_id"])

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(f"SELECT COUNT(*) AS n FROM signal_facts {where}", params).fetchone()["n"]
    sort_cols = {"observed_on", "symbol", "signal", "state", "triggered"}
    sort_col = sort if sort in sort_cols else "observed_on"
    asc = "ASC" if order == "asc" else "DESC"
    offset = (page - 1) * page_size
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT fact_id, symbol, observed_on, signal, state, anchor_id, triggered,
               active_until, details_json, run_id, rule_version, config_hash, created_at
        FROM signal_facts {where} ORDER BY {sort_col} {asc}, fact_id DESC LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    )
    for r in rows:
        r["triggered"] = bool(r["triggered"])
        r["details"] = _safe_json(r["details_json"])
    return {"page": page, "page_size": page_size, "total": total, "items": rows}


def get_signal_details(conn: sqlite3.Connection, fact_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM signal_facts WHERE fact_id = ?", (fact_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["triggered"] = bool(d["triggered"])
    d["details"] = _safe_json(d["details_json"])
    del d["details_json"]
    if d["anchor_id"]:
        a = conn.execute("SELECT * FROM weekly_anchors WHERE anchor_id = ?",
                         (d["anchor_id"],)).fetchone()
        d["anchor"] = dict(a) if a else None
    else:
        d["anchor"] = None
    return d


# ---------------------------------------------------------------- 卡片

def _parse_card(d: dict) -> dict:
    for f in _CARD_PARSE_FIELDS:
        if f in d:
            d[f] = _safe_json(d[f])
    return d


def _tier_summary(price_tiers: dict | None) -> list[tuple]:
    if not price_tiers:
        return []
    return [(t["tier"], t["zone_low"], t["zone_high"])
            for t in price_tiers.get("tiers", [])]


def list_cards(conn: sqlite3.Connection, filters: dict | None = None,
               page: int = 1, page_size: int = 50,
               sort: str = "created_at", order: str = "desc") -> dict:
    filters = filters or {}
    clauses, params = [], []
    if filters.get("symbol"):
        clauses.append("c.symbol = ?")
        params.append(filters["symbol"])
    if filters.get("status"):
        statuses = _as_list(filters["status"])
        clauses.append(f"c.status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    ef, et = filters.get("effective_from"), filters.get("effective_to")
    if ef or et:
        # 区间筛选只针对有生效起点的卡片（draft 无生效日不计入）
        clauses.append("c.effective_from IS NOT NULL")
    if ef:
        clauses.append("(c.effective_to IS NULL OR c.effective_to >= ?)")
        params.append(ef)
    if et:
        clauses.append("(c.effective_from IS NULL OR c.effective_from <= ?)")
        params.append(et)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM strategy_card_versions c {where}", params).fetchone()["n"]
    sort_cols = {"created_at", "effective_from", "status"}
    sort_col = sort if sort in sort_cols else "created_at"
    asc = "ASC" if order == "asc" else "DESC"
    offset = (page - 1) * page_size
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT c.card_version_id, c.symbol, c.status, c.effective_from, c.effective_to,
               c.supersedes_id, c.next_review_at, c.created_at, c.run_id, c.price_basis,
               c.price_tiers_json, w.name AS name
        FROM strategy_card_versions c
        LEFT JOIN watchlist w ON w.symbol = c.symbol
        {where} ORDER BY c.{sort_col} {asc} LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    )
    for r in rows:
        tiers = _safe_json(r["price_tiers_json"])
        r["tier_summary"] = _tier_summary(tiers)
        del r["price_tiers_json"]
    return {"page": page, "page_size": page_size, "total": total, "items": rows}


def get_card_detail(conn: sqlite3.Connection, card_version_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM strategy_card_versions WHERE card_version_id = ?", (card_version_id,)
    ).fetchone()
    if row is None:
        return None
    d = _parse_card(dict(row))
    d["tier_summary"] = _tier_summary(d.get("price_tiers_json"))
    # 版本链：沿 supersedes_id 递归向上
    chain = [d["card_version_id"]]
    cur = d["supersedes_id"]
    while cur and cur not in chain:
        chain.append(cur)
        prev = conn.execute("SELECT * FROM strategy_card_versions WHERE card_version_id = ?",
                            (cur,)).fetchone()
        if prev is None:
            break
        cur = prev["supersedes_id"]
    d["version_chain"] = chain
    return d


# ---------------------------------------------------------------- 执行记录

def list_executions(conn: sqlite3.Connection, symbol: str | None = None,
                    start: str | None = None, end: str | None = None,
                    page: int = 1, page_size: int = 50) -> dict:
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if start:
        clauses.append("executed_at >= ?")
        params.append(start)
    if end:
        # 日期区间按 UTC 时间戳前缀过滤，end 取次日零时
        next_day = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        clauses.append("executed_at < ?")
        params.append(next_day)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(f"SELECT COUNT(*) AS n FROM executions {where}", params).fetchone()["n"]
    offset = (page - 1) * page_size
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT execution_id, idempotency_key, symbol, executed_at, action_type, tier,
               price, quantity, fees, card_version_id, signal_snapshot_json,
               reverses_execution_id, created_at
        FROM executions {where} ORDER BY executed_at DESC LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    )
    return {"page": page, "page_size": page_size, "total": total, "items": rows}


# ---------------------------------------------------------------- 运行记录

def _paginate(conn: sqlite3.Connection, table: str, where: str, params: list,
              page: int, page_size: int, sort_col: str, order: str,
              extra_select: str = "") -> dict:
    total = conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}", params).fetchone()["n"]
    asc = "ASC" if order == "asc" else "DESC"
    offset = (page - 1) * page_size
    rows = _fetch_dicts(
        conn,
        f"SELECT * {extra_select} FROM {table} {where} ORDER BY {sort_col} {asc} "
        f"LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    return {"page": page, "page_size": page_size, "total": total, "items": rows}


def list_pipeline_runs(conn: sqlite3.Connection, filters: dict | None = None,
                       page: int = 1, page_size: int = 50,
                       sort: str = "started_at", order: str = "desc") -> dict:
    filters = filters or {}
    clauses, params = [], []
    if filters.get("run_id"):
        clauses.append("run_id LIKE ?")
        params.append(f"%{filters['run_id']}%")
    if filters.get("stage"):
        clauses.append("stage LIKE ?")
        params.append(f"%{filters['stage']}%")
    if filters.get("status"):
        statuses = _as_list(filters["status"])
        clauses.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if filters.get("start"):
        clauses.append("started_at >= ?")
        params.append(filters["start"])
    if filters.get("end"):
        clauses.append("started_at < ?")
        params.append((date.fromisoformat(filters["end"]) + timedelta(days=1)).isoformat())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    data = _paginate(conn, "pipeline_runs", where, params, page, page_size,
                     sort if sort in {"started_at", "status", "stage"} else "started_at", order)
    for r in data["items"]:
        dur = None
        if r.get("started_at") and r.get("finished_at"):
            try:
                s = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
                f = datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00"))
                dur = round((f - s).total_seconds(), 2)
            except ValueError:
                dur = None
        r["duration_sec"] = dur
        if r.get("config_hash"):
            r["config_hash_short"] = r["config_hash"][:8]
    return data


def list_report_runs(conn: sqlite3.Connection, filters: dict | None = None,
                     page: int = 1, page_size: int = 50,
                     sort: str = "created_at", order: str = "desc") -> dict:
    filters = filters or {}
    clauses, params = [], []
    if filters.get("report_run_id"):
        clauses.append("report_run_id = ?")
        params.append(filters["report_run_id"])
    if filters.get("report_type"):
        clauses.append("report_type = ?")
        params.append(filters["report_type"])
    if filters.get("symbol"):
        clauses.append("symbol = ?")
        params.append(filters["symbol"])
    if filters.get("trade_date"):
        clauses.append("trade_date = ?")
        params.append(filters["trade_date"])
    if filters.get("status"):
        statuses = _as_list(filters["status"])
        clauses.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    data = _paginate(conn, "report_runs", where, params, page, page_size,
                     sort if sort in {"created_at", "trade_date", "status"} else "created_at", order)
    for r in data["items"]:
        if r.get("config_hash"):
            r["config_hash_short"] = r["config_hash"][:8]
    return data


# ---------------------------------------------------------------- 辅助查询

def get_trading_dates(conn: sqlite3.Connection, market: str,
                      start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT trade_date FROM trading_calendar
        WHERE market = ? AND is_open = 1 AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (market, start, end),
    ).fetchall()
    return [r[0] for r in rows]


def get_latest_trade_date(conn: sqlite3.Connection, market: str) -> str:
    row = conn.execute(
        "SELECT MAX(trade_date) AS d FROM trading_calendar WHERE market = ? AND is_open = 1",
        (market,),
    ).fetchone()
    return row["d"]


def get_benchmark_bars(conn: sqlite3.Connection, symbol: str,
                       start: str | None = None, end: str | None = None) -> list[dict]:
    clauses, params = ["index_code = ?"], [symbol]
    if start:
        clauses.append("trade_date >= ?")
        params.append(start)
    if end:
        clauses.append("trade_date <= ?")
        params.append(end)
    where = " AND ".join(clauses)
    return _fetch_dicts(
        conn,
        f"""
        SELECT trade_date, open, high, low, close, volume, currency
        FROM index_bars WHERE {where} ORDER BY trade_date ASC
        """,
        params,
    )


# ---------------------------------------------------------------- 多股指标/对比

def get_multi_indicators(conn: sqlite3.Connection, symbols: list[str], granularity: str,
                         start: str | None, end: str | None, fields: list[str],
                         price: str = "unadjusted") -> dict:
    """多股多指标按日期对齐（docs/ui_design_phase1.md §4.4 / task-05）。

    返回 {symbols, granularity, start, end, fields, series}，series[field][date][symbol] = value。
    """
    if len(symbols) > 6:
        raise ValueError("最多 6 只股票")
    if len(fields) > 6:
        raise ValueError("最多 6 个指标")
    series: dict[str, dict[str, dict[str, float]]] = {f: {} for f in fields}
    for sym in symbols:
        rows = get_stock_indicators(conn, sym, granularity, start, end, fields, price)
        for r in rows:
            for f in fields:
                if r[f] is not None:
                    series[f].setdefault(r["date"], {})[sym] = r[f]
    return {"symbols": symbols, "granularity": granularity,
            "start": start, "end": end, "fields": fields, "series": series}


def get_compare(conn: sqlite3.Connection, symbols: list[str], metric: str,
                granularity: str, start: str | None, end: str | None,
                price: str = "unadjusted") -> dict:
    """多股单指标对比（docs/ui_design_phase1.md §4.7 / task-07）。"""
    if not (2 <= len(symbols) <= 6):
        raise ValueError("对比需要 2~6 只股票")
    allowed = set(_indicator_columns(conn, _INDICATOR_TABLES[granularity][0]))
    bar_metrics = {"close", "volume", "amount"}
    if metric not in allowed | bar_metrics:
        raise ValueError(f"unknown indicator field: {metric}")
    if metric in bar_metrics:
        # close/volume 为价格/量能列，走 bars 口径（含 fully_adjusted 与周线聚合）
        dates = sorted({r["trade_date"] for r in get_stock_bars(
            conn, symbols[0], granularity, start, end, price)})
        series: dict[str, list[float | None]] = {s: [] for s in symbols}
        for s in symbols:
            bars = {b["trade_date"]: b[metric] for b in get_stock_bars(
                conn, s, granularity, start, end, price)}
            series[s] = [bars.get(d) for d in dates]
    else:
        rows_map: dict[str, dict[str, float]] = {}
        for s in symbols:
            for r in get_stock_indicators(conn, s, granularity, start, end, [metric], price):
                rows_map.setdefault(s, {})[r["date"]] = r[metric]
        dates = sorted({d for m in rows_map.values() for d in m})
        series = {s: [rows_map[s].get(d) for d in dates] for s in symbols}
    metadata = {
        s: {"name": (conn.execute("SELECT name FROM watchlist WHERE symbol = ?", (s,)).fetchone()
                     or ({"name": s}))["name"]}
        for s in symbols
    }
    return {"symbols": symbols, "metric": metric, "granularity": granularity,
            "dates": dates, "series": series, "metadata": metadata}


# ---------------------------------------------------------------- 仪表板

def get_dashboard(conn: sqlite3.Connection) -> dict:
    """首页概览（docs/ui_design_phase1.md §3.1 / task-10）。"""
    markets = list_markets(conn)
    total_stocks = conn.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]

    stocks_with_data_today = conn.execute(
        "SELECT COUNT(DISTINCT symbol) AS n FROM daily_bars "
        "WHERE trade_date = (SELECT MAX(trade_date) FROM daily_bars)").fetchone()["n"]
    stocks_with_active_card = conn.execute(
        "SELECT COUNT(DISTINCT symbol) AS n FROM strategy_card_versions "
        "WHERE status = 'active'").fetchone()["n"]
    stocks_with_signal_today = conn.execute(
        "SELECT COUNT(DISTINCT symbol) AS n FROM signal_facts "
        "WHERE triggered = 1 AND observed_on = "
        "(SELECT MAX(observed_on) FROM signal_facts WHERE triggered = 1)").fetchone()["n"]

    latest_trade_date = conn.execute("SELECT MAX(trade_date) AS d FROM daily_bars").fetchone()["d"]

    latest_run = conn.execute(
        "SELECT run_id, status, started_at, finished_at FROM pipeline_runs "
        "ORDER BY started_at DESC LIMIT 1").fetchone()
    latest_run = dict(latest_run) if latest_run else None

    run_stats = {k: 0 for k in ("success", "degraded", "failed", "running")}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM pipeline_runs GROUP BY status"):
        if r["status"] in run_stats:
            run_stats[r["status"]] = r["n"]

    trade_dates = {}
    for m in markets:
        trade_dates[m] = {"latest": get_latest_trade_date(conn, m)}

    return {
        "markets": markets,
        "total_stocks": total_stocks,
        "stocks_with_data_today": stocks_with_data_today,
        "stocks_with_active_card": stocks_with_active_card,
        "stocks_with_signal_today": stocks_with_signal_today,
        "latest_trade_date": latest_trade_date,
        "latest_run": latest_run,
        "run_stats": run_stats,
        "trade_dates": trade_dates,
        "alerts": get_dashboard_alerts(conn),
        "run_trend": get_run_stats(conn, days=7),
    }


def get_dashboard_alerts(conn: sqlite3.Connection) -> list[dict]:
    """异常清单：failed 运行 / incomplete 信号 / pe_status 降级 / 停牌 / 复核到期。"""
    alerts: list[dict] = []
    now = date.today().isoformat()

    for r in conn.execute(
        "SELECT stage, status, error, run_id, started_at FROM pipeline_runs "
        "WHERE status IN ('failed','degraded') ORDER BY started_at DESC LIMIT 20"
    ):
        symbol = None
        stage = r["stage"]
        if stage.startswith("symbol:"):
            symbol = stage.split(":", 1)[1]
        alerts.append({
            "symbol": symbol, "stage": stage,
            "type": "run_" + r["status"], "message": r["error"] or stage,
            "run_id": r["run_id"], "at": r["started_at"],
        })

    for r in conn.execute(
        "SELECT symbol, observed_on, signal, details_json FROM signal_facts "
        "WHERE state = 'incomplete' ORDER BY observed_on DESC LIMIT 20"
    ):
        alerts.append({
            "symbol": r["symbol"], "type": "signal_incomplete",
            "message": f"{r['signal']} incomplete（{r['observed_on']}）",
            "observed_on": r["observed_on"],
        })

    for r in conn.execute(
        "SELECT DISTINCT symbol, trade_date, pe_status FROM indicators_daily "
        "WHERE pe_status IS NOT NULL AND pe_status != '' "
        "AND trade_date = (SELECT MAX(trade_date) FROM indicators_daily i2 "
        "WHERE i2.symbol = indicators_daily.symbol)"
    ):
        alerts.append({
            "symbol": r["symbol"], "type": "pe_status",
            "message": f"PE 状态：{r['pe_status']}", "observed_on": r["trade_date"],
        })

    for r in conn.execute(
        "SELECT symbol, trade_date FROM daily_bars "
        "WHERE trading_status = 'suspended' "
        "AND trade_date = (SELECT MAX(trade_date) FROM daily_bars d2 "
        "WHERE d2.symbol = daily_bars.symbol)"
    ):
        alerts.append({"symbol": r["symbol"], "type": "suspended",
                       "message": "停牌", "observed_on": r["trade_date"]})

    for r in conn.execute(
        "SELECT symbol, card_version_id, next_review_at FROM strategy_card_versions "
        "WHERE status = 'active' AND next_review_at IS NOT NULL AND next_review_at <= ?",
        (now,),
    ):
        alerts.append({"symbol": r["symbol"], "type": "review_due",
                       "message": f"复核到期 {r['next_review_at']}",
                       "card_version_id": r["card_version_id"]})

    return alerts


def get_run_stats(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    """最近 N 天按天聚合的运行状态趋势。"""
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        """
        SELECT date(started_at) AS day, status, COUNT(*) AS n
        FROM pipeline_runs WHERE started_at >= ? GROUP BY day, status
        """,
        (start,),
    ).fetchall()
    trend = []
    for d in range(days):
        day = (date.today() - timedelta(days=days - 1 - d)).isoformat()
        bucket = {"date": day, "success": 0, "degraded": 0, "failed": 0, "other": 0}
        for r in rows:
            if r["day"] == day:
                key = r["status"] if r["status"] in bucket else "other"
                bucket[key] = r["n"]
        trend.append(bucket)
    return trend


# ---------------------------------------------------------------- 工具

def _safe_json(s: str | None):
    if s is None or s == "":
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {"_raw": s}
