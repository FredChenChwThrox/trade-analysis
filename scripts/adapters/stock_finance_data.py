"""stock_finance_data adapter（设计 §3.1-3.2、§3.5-3.7）。

覆盖数据类型：
- price        行情 CSV → daily_bars（映射见 docs/probe_20260809_stock_finance_data.md）
- financials   利润表 CSV → financial_reports + financial_facts（更正新增 revision，不覆盖）
- announcement 公告 CSV → events + event_symbols（event_type='announcement'）
- forecast     一致预期 CSV → forecasts（每次抓取一批快照）
- index        指数日线 CSV → index_bars

已知来源偏差（详见 docs/execution_log.md 2026-08-09 D1.3 条目）：
- 行情无 amount 列 → amount_raw = NULL（probe01 已确认）；
- 复权因子列本批填 1.0，由 D1.5 复权模块重算；
- 利润表无披露时间 → available_at 降级取入库时间并记 incomplete；
- 利润表无股本列 → shares_issued_end / shares_float_end 为 NULL；
- 公告接口实测 EMPTY_DATA，列名按接口文档描述推断，未经过真实样本验证。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.adapters.common import (
    IngestResult,
    dec_str,
    load_calendar,
    market_of,
    market_tz,
    record_revision,
    utc_now,
)

SOURCE = "stock_finance_data"

_FLOAT_TOL = 1e-6  # OHLC 一致性校验容差（两位小数舍入噪声）


def vals_equal(a, b) -> bool:
    """None 安全 + 浮点容差的值比较（用于判定规范化事实是否变化）。"""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return abs(a - b) <= _FLOAT_TOL
    return a == b


# ---------------------------------------------------------------- 行情 → daily_bars

def _validate_bar_row(row: dict, trade_date: str) -> str | None:
    """单行 OHLC 校验（§3.2）。返回 None 表示通过，否则返回失败原因。

    O/H/L/C 全部缺失 → 返回 'EMPTY'（行级跳过，不算冲突：实时末根 bar 常见残缺）。
    """
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    if all(v is None for v in (o, h, l, c)):
        return "EMPTY"
    if any(v is None for v in (o, h, l, c)):
        return f"{trade_date}: OHLC 部分缺失 o={o} h={h} l={l} c={c}"
    if min(o, h, l, c) < 0:
        return f"{trade_date}: 价格为负 o={o} h={h} l={l} c={c}"
    if not (l - _FLOAT_TOL <= min(o, c) and max(o, c) <= h + _FLOAT_TOL):
        return f"{trade_date}: 违反 low<=open/close<=high o={o} h={h} l={l} c={c}"
    vol = row.get("volume")
    if vol is not None and vol < 0:
        return f"{trade_date}: 成交量为负 volume={vol}"
    amt = row.get("amount")
    if amt is not None and amt < 0:
        return f"{trade_date}: 成交额为负 amount={amt}"
    return None


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    if s in ("", "NA", "nan"):
        return None
    return float(s)


def upsert_daily_bars(
    conn: sqlite3.Connection,
    bars: list[dict],
    *,
    source: str,
    raw_object_id: str,
    run_id: str | None,
    result: IngestResult,
) -> None:
    """批量 upsert daily_bars；内容变化记 data_revisions（§3.2、§9.5）。

    bars 元素：symbol, trade_date, market, open/high/low/close/volume/amount, currency
    """
    now = utc_now()
    for bar in bars:
        key = (bar["symbol"], bar["trade_date"])
        old = conn.execute(
            "SELECT * FROM daily_bars WHERE symbol = ? AND trade_date = ?", key
        ).fetchone()
        # 新 bar 因子继承上一交易日：无除权时因子沿平台延续（填 1.0 会让
        # 因子变化检查把新 bar 误判为窗口内除权）；真除权时检查触发全量重建，
        # 由 D1.5 复权模块统一重算。无历史（新股）时落 1.0。
        prev = conn.execute(
            "SELECT price_adj_factor, share_factor FROM daily_bars "
            "WHERE symbol = ? AND trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1", key
        ).fetchone()
        new_row = (
            bar["symbol"], bar["trade_date"], bar["market"],
            bar["open"], bar["high"], bar["low"], bar["close"],
            bar.get("volume"), bar.get("amount"), bar.get("currency"),
            prev["price_adj_factor"] if prev else 1.0,
            prev["share_factor"] if prev else 1.0,
            "normal", source, raw_object_id, now,
        )
        if old is None:
            conn.execute(
                """
                INSERT INTO daily_bars (symbol, trade_date, market,
                    open_raw, high_raw, low_raw, close_raw,
                    volume_raw, amount_raw, currency,
                    price_adj_factor, share_factor, trading_status,
                    source, raw_object_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                new_row,
            )
            result.inserted += 1
            continue
        old_vals = tuple(
            old[c] for c in (
                "open_raw", "high_raw", "low_raw", "close_raw",
                "volume_raw", "amount_raw", "currency",
            )
        )
        new_vals = new_row[3:10]
        if all(vals_equal(a, b) for a, b in zip(old_vals, new_vals)):
            result.skipped += 1
            continue
        conn.execute(
            """
            UPDATE daily_bars SET open_raw=?, high_raw=?, low_raw=?, close_raw=?,
                volume_raw=?, amount_raw=?, currency=?,
                trading_status='normal', source=?, raw_object_id=?, updated_at=?
            WHERE symbol=? AND trade_date=?
            """,
            (*new_row[3:10], source, raw_object_id, now, *key),
        )
        record_revision(
            conn, table_name="daily_bars",
            record_key={"symbol": key[0], "trade_date": key[1]},
            old_value=dict(zip(
                ("open_raw", "high_raw", "low_raw", "close_raw",
                 "volume_raw", "amount_raw", "currency"), old_vals)),
            new_value=dict(zip(
                ("open_raw", "high_raw", "low_raw", "close_raw",
                 "volume_raw", "amount_raw", "currency"), new_vals)),
            source=source, reason="source_revision", run_id=run_id,
        )
        result.updated += 1


def parse_price_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """stock_finance_data 行情 CSV → daily_bars。

    列：open,high,low,close,volume,thscode,time,thsname_cn,thsname_en,currency
    （amount 列缺失 → amount_raw NULL；adjust=forward 的 *_forward 文件不入 daily_bars，
    由 ingest CLI 路由时排除，留给 D1.5 复权模块）。
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
            raw_time = (rec.get("time") or "").strip()
            try:
                trade_date = datetime.strptime(raw_time, "%Y%m%d").date().isoformat()
            except ValueError:
                result.conflicts += 1
                result.errors.append(f"time 列无法解析: {raw_time!r}")
                return result
            market = market_of(symbol)
            if market not in calendar_cache:
                calendar_cache[market] = load_calendar(conn, market)
            calendar = calendar_cache[market]
            if not calendar:
                # 该市场日历缺失：不猜，记 incomplete（§2.5），跳过交易日校验
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
                "open": _num(rec.get("open")), "high": _num(rec.get("high")),
                "low": _num(rec.get("low")), "close": _num(rec.get("close")),
                "volume": _num(rec.get("volume")), "amount": _num(rec.get("amount")),
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


# ---------------------------------------------------------------- 指数 → index_bars

def upsert_index_bars(conn: sqlite3.Connection, bars: list[dict], *,
                      source: str, raw_object_id: str, run_id: str | None,
                      result: IngestResult) -> None:
    now = utc_now()
    for bar in bars:
        key = (bar["index_code"], bar["trade_date"])
        old = conn.execute(
            "SELECT * FROM index_bars WHERE index_code = ? AND trade_date = ?", key
        ).fetchone()
        vals = (bar["open"], bar["high"], bar["low"], bar["close"],
                bar.get("volume"), bar.get("currency"))
        if old is None:
            conn.execute(
                """
                INSERT INTO index_bars (index_code, trade_date, open, high, low, close,
                                        volume, currency, source, available_at,
                                        raw_object_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*key, *vals, source, now, raw_object_id, now),
            )
            result.inserted += 1
        else:
            old_vals = tuple(old[c] for c in
                             ("open", "high", "low", "close", "volume", "currency"))
            if all(vals_equal(a, b) for a, b in zip(old_vals, vals)):
                result.skipped += 1
            else:
                conn.execute(
                    """
                    UPDATE index_bars SET open=?, high=?, low=?, close=?,
                        volume=?, currency=?, source=?, raw_object_id=?, updated_at=?
                    WHERE index_code=? AND trade_date=?
                    """,
                    (*vals, source, raw_object_id, now, *key),
                )
                record_revision(
                    conn, table_name="index_bars",
                    record_key={"index_code": key[0], "trade_date": key[1]},
                    old_value=dict(zip(("open", "high", "low", "close", "volume", "currency"), old_vals)),
                    new_value=dict(zip(("open", "high", "low", "close", "volume", "currency"), vals)),
                    source=source, reason="source_revision", run_id=run_id,
                )
                result.updated += 1


def parse_index_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                    result: IngestResult) -> IngestResult:
    """stock_finance_data 指数日线 CSV → index_bars（列同行情 CSV）。"""
    bars: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            code = (rec.get("thscode") or "").strip()
            raw_time = (rec.get("time") or "").strip()
            try:
                trade_date = datetime.strptime(raw_time, "%Y%m%d").date().isoformat()
            except ValueError:
                result.conflicts += 1
                result.errors.append(f"time 列无法解析: {raw_time!r}")
                return result
            row = {
                "open": _num(rec.get("open")), "high": _num(rec.get("high")),
                "low": _num(rec.get("low")), "close": _num(rec.get("close")),
                "volume": _num(rec.get("volume")),
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


# ---------------------------------------------------------------- 利润表 → financial_reports/facts

_PERIOD_RE = re.compile(r"_is_(\d{8})")

_PERIOD_TYPE = {"1231": "annual", "0630": "interim", "0331": "quarterly", "0930": "quarterly"}

_FACT_FIELDS = {
    "revenue": ("ths_operating_total_revenue_stock", "ths_revenue_stock"),
    "net_profit_attr": ("ths_np_atoopc_stock",),
    "eps_basic": ("ths_basic_eps_stock",),
    "eps_diluted": ("ths_dlt_earnings_per_share_stock",),
}


def _period_from_filename(path: Path) -> str | None:
    m = _PERIOD_RE.search(path.name)
    if not m:
        return None
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def parse_financials_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                         result: IngestResult) -> IngestResult:
    """利润表 CSV → financial_reports + financial_facts。

    相同报告期内容变化 → 新增 revision 行，不覆盖旧版本（§3.7 硬门槛）。
    period_end 从文件名 `_is_YYYYMMDD` 推断（CSV 内 time 列为空，实测见执行日志）。
    """
    period_end = _period_from_filename(path)
    if period_end is None:
        result.conflicts += 1
        result.errors.append(f"文件名不含 _is_YYYYMMDD 报告期: {path.name}")
        return result

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.conflicts += 1
        result.errors.append("利润表 CSV 无数据行")
        return result
    rec = rows[0]
    symbol = (rec.get("thscode") or "").strip()
    if not symbol:
        result.conflicts += 1
        result.errors.append("缺少 thscode 列或值为空")
        return result

    mmdd = period_end[5:7] + period_end[8:10]
    period_type = _PERIOD_TYPE.get(mmdd, "quarterly")
    fiscal_year = int(period_end[:4])
    is_cumulative = 1  # A 股利润表为累计值

    facts: dict[str, str | None] = {}
    for field_name, candidates in _FACT_FIELDS.items():
        value = None
        for col in candidates:
            value = dec_str(rec.get(col))
            if value is not None:
                break
        facts[field_name] = value

    existing = conn.execute(
        """
        SELECT r.report_id, r.revision, f.revenue, f.net_profit_attr,
               f.eps_basic, f.eps_diluted
        FROM financial_reports r
        JOIN financial_facts f ON f.report_id = r.report_id
        WHERE r.symbol = ? AND r.period_end = ? AND r.period_type = ?
          AND r.is_cumulative = ?
        ORDER BY r.revision DESC
        """,
        (symbol, period_end, period_type, is_cumulative),
    ).fetchall()

    if existing:
        latest = existing[0]
        same = all(latest[k] == facts[k] for k in _FACT_FIELDS)
        if same:
            result.skipped += 1
            result.notes.append(
                f"{symbol} {period_end} 报告内容一致，跳过（已有 revision={latest['revision']}）")
            return result
        revision = latest["revision"] + 1  # 更正：新增 revision，不覆盖
        result.notes.append(
            f"{symbol} {period_end} 检测到更正，新增 revision={revision}")
    else:
        revision = 1

    now = utc_now()
    # 来源无披露时间（CSV 无该列）：available_at 降级取入库时间（§2.1 降级，记 incomplete）
    result.incomplete_reasons.append(
        f"{symbol} {period_end}: 来源无披露时间，available_at 取入库时间（降级）")
    cur = conn.execute(
        """
        INSERT INTO financial_reports (symbol, period_end, period_type, fiscal_year,
            published_at, published_tz, available_at, revision,
            currency, unit, is_cumulative, raw_object_id, ingested_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, period_end, period_type, fiscal_year,
         "Asia/Shanghai", now, revision,
         "CNY", "yuan", is_cumulative, raw_object_id, now),
    )
    report_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO financial_facts (report_id, revenue, net_profit_attr,
            eps_basic, eps_diluted, shares_issued_end, shares_float_end,
            share_count_type, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
        """,
        (report_id, facts["revenue"], facts["net_profit_attr"],
         facts["eps_basic"], facts["eps_diluted"], now),
    )
    result.inserted += 1
    return result


# ---------------------------------------------------------------- 公告 → events/event_symbols

# ⚠️ 公告接口实测 EMPTY_DATA（2026-08-09，603605.SH 与 600519.SH 近 3 个月均空），
# 以下列名按接口文档描述（日期/发布时间/标题/PDF URL/序号）推断，未经真实样本验证。
_ANN_COL_CANDIDATES = {
    "date": ("time", "date", "announcement_date", "公告日期"),
    "title": ("title", "announcement_title", "公告标题"),
    "url": ("url", "pdf_url", "announcement_url", "公告链接"),
    "seq": ("seq", "sequence", "announcement_id", "id", "公告序号"),
    "symbol": ("thscode", "ticker", "symbol"),
}


def _pick(rec: dict, candidates: tuple[str, ...]) -> str | None:
    lower = {k.lower(): v for k, v in rec.items() if k}
    for c in candidates:
        if c.lower() in lower and (lower[c.lower()] or "").strip():
            return lower[c.lower()].strip()
    return None


def parse_announcement_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                           result: IngestResult) -> IngestResult:
    """公告 CSV → events + event_symbols（event_type='announcement'）。

    去重：来源序号（source_external_id）优先，其次标题+日期哈希（§3.6）。
    available_at：发布时间只有日期粒度时，取下一个开市交易日（§2.1：不假定盘前发布）。
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("公告 CSV 无数据行")
        return result

    calendar_cache: dict[str, dict] = {}
    now = utc_now()
    for rec in rows:
        title = _pick(rec, _ANN_COL_CANDIDATES["title"])
        date_s = _pick(rec, _ANN_COL_CANDIDATES["date"])
        symbol = _pick(rec, _ANN_COL_CANDIDATES["symbol"])
        if not title or not date_s:
            result.conflicts += 1
            result.errors.append(
                f"公告行缺标题或日期列（实得列: {sorted(rec.keys())}）")
            return result
        pub_date = date_s[:10]
        external_id = _pick(rec, _ANN_COL_CANDIDATES["seq"])
        url = _pick(rec, _ANN_COL_CANDIDATES["url"])
        dedup_key = external_id or f"{title}|{pub_date}"
        event_id = "evt_" + hashlib.sha256(
            f"{SOURCE}|{dedup_key}".encode()).hexdigest()[:16]

        if conn.execute("SELECT 1 FROM events WHERE event_id = ?",
                        (event_id,)).fetchone():
            result.skipped += 1
            continue

        tz = market_tz("CN")
        published_at = datetime.fromisoformat(pub_date).replace(
            tzinfo=tz).astimezone(timezone.utc).isoformat()
        if "CN" not in calendar_cache:
            calendar_cache["CN"] = load_calendar(conn, "CN")
        calendar = calendar_cache["CN"]
        if calendar:
            # 下一个开市交易日 00:00（本地）作为 available_at（§2.1）
            d = datetime.fromisoformat(pub_date).date() + timedelta(days=1)
            while d.isoformat() not in calendar or not calendar[d.isoformat()]["is_open"]:
                d += timedelta(days=1)
                if (d - datetime.fromisoformat(pub_date).date()).days > 40:
                    break
            available_at = datetime(d.year, d.month, d.day,
                                    tzinfo=tz).astimezone(timezone.utc).isoformat()
        else:
            result.incomplete_reasons.append(
                "trading_calendar 缺失（market=CN），available_at 取发布日+1 天（降级）")
            d = datetime.fromisoformat(pub_date).date() + timedelta(days=1)
            available_at = datetime(d.year, d.month, d.day,
                                    tzinfo=tz).astimezone(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO events (event_id, event_type, event_at, published_at,
                published_tz, available_at, title, summary, canonical_url,
                source, source_external_id, content_hash, raw_object_id, ingested_at)
            VALUES (?, 'announcement', NULL, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, published_at, "Asia/Shanghai", available_at, title, url,
             SOURCE, external_id,
             hashlib.sha256(f"{title}|{pub_date}".encode()).hexdigest(),
             raw_object_id, now),
        )
        if symbol:
            conn.execute(
                "INSERT OR IGNORE INTO event_symbols (event_id, symbol) VALUES (?, ?)",
                (event_id, symbol),
            )
        result.inserted += 1
    return result


# ---------------------------------------------------------------- 一致预期 → forecasts

def parse_forecast_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                       result: IngestResult) -> IngestResult:
    """一致预期 CSV → forecasts（每次抓取一批快照，全量保存；§3.7）。

    payload_json 保存当次 CSV 全量行；历史查询取 snapshot_at <= as_of 最新快照。
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = [dict(r) for r in csv.DictReader(f)]
    if not rows:
        result.conflicts += 1
        result.errors.append("预期 CSV 无数据行")
        return result
    symbol = None
    for r in rows:
        if (r.get("thscode") or "").strip():
            symbol = r["thscode"].strip()
            break
    if not symbol:
        result.conflicts += 1
        result.errors.append("预期 CSV 缺少 thscode")
        return result
    now = utc_now()
    conn.execute(
        """
        INSERT INTO forecasts (symbol, snapshot_at, source, payload_json,
                               raw_object_id, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (symbol, now, SOURCE,
         json.dumps({"rows": rows}, ensure_ascii=False), raw_object_id, now),
    )
    result.inserted += 1
    return result
