"""UI 测试用种子数据：在 db.migrate+seed 之后插入合成的行情/指标/信号/卡片/运行记录。

所有 UI 查询测试共用同一份确定性数据：
- 603605.SH  珀莱雅  因子 1.0，30 个交易日，有 active 卡片 + 触发信号 + 执行记录
- 002747.SZ  埃斯顿  因子 2.0，30 个交易日，无卡片，最新运行 failed
- 0700.HK    腾讯    因子 1.0，15 个交易日，pe_status 非空（数据质量降级）
"""

from __future__ import annotations

import datetime as dt
import json

import sqlite3

TRADE_END = dt.date(2026, 8, 7)
N_DAYS = 30


def _weekdays(end: dt.date, n: int) -> list[str]:
    dates: list[dt.date] = []
    d = end
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d -= dt.timedelta(days=1)
    dates.sort()
    return [x.isoformat() for x in dates]


def _ma(values: list[float], i: int, n: int) -> float | None:
    if i + 1 < n:
        return None
    return round(sum(values[i - n + 1 : i + 1]) / n, 6)


def _seed_watchlist_hk(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,
                               currency, timezone, active, created_at, updated_at)
        VALUES ('0700.HK', 'HK', '腾讯控股', '[]', '^HSI', 'HKD', 'Asia/Hong_Kong',
                1, '2026-08-10T00:00:00+00:00', '2026-08-10T00:00:00+00:00')
        """
    )


def _seed_bars(conn: sqlite3.Connection) -> dict[str, list[str]]:
    dates = _weekdays(TRADE_END, N_DAYS)
    now = "2026-08-10T00:00:00+00:00"
    rows: list[tuple] = []
    for symbol, base, factor, n in [
        ("603605.SH", 50.0, 1.0, N_DAYS),
        ("002747.SZ", 20.0, 2.0, N_DAYS),
        ("0700.HK", 300.0, 1.0, 15),
    ]:
        for i, d in enumerate(dates[:n]):
            close = base + i
            rows.append((
                symbol, d, "CN" if symbol.endswith(".SH") or symbol.endswith(".SZ") else "HK",
                round(close - 0.5, 4), round(close + 1.0, 4), round(close - 1.0, 4),
                round(close, 4), 1_000_000 + i * 1000, None, "CNY", factor, 1.0,
                "normal", "test", None, now,
            ))
    conn.executemany(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw, low_raw,
                                close_raw, volume_raw, amount_raw, currency,
                                price_adj_factor, share_factor, trading_status,
                                source, raw_object_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return {"dates": dates}


def _seed_indicators(conn: sqlite3.Connection, dates: list[str]) -> None:
    now = "2026-08-10T00:00:00+00:00"
    rows: list[tuple] = []
    for symbol, base, factor, pe, pe_status, pct in [
        ("603605.SH", 50.0, 1.0, 15.0, "", -0.5),
        ("002747.SZ", 20.0, 2.0, 25.0, "", 1.2),
        ("0700.HK", 300.0, 1.0, None, "no_share_capital", 0.3),
    ]:
        n = 15 if symbol == "0700.HK" else N_DAYS
        closes = [base + i for i in range(n)]
        for i, d in enumerate(dates[:n]):
            ma5 = _ma(closes, i, 5)
            ma10 = _ma(closes, i, 10)
            ma20 = _ma(closes, i, 20)
            # 指标以复权价计算：存储值 = 不复权口径 × 因子
            ma5_adj = ma5 * factor if ma5 is not None else None
            ma10_adj = ma10 * factor if ma10 is not None else None
            ma20_adj = ma20 * factor if ma20 is not None else None
            rows.append((
                symbol, d,
                ma5_adj, ma10_adj, ma20_adj, None, None, None,   # ma5..ma250
                None, None, None, None, None, None,          # dif..rsi24
                ma5_adj, None, None, None,                   # boll_*
                None, None, None, None, None, None,          # vol_*
                None, None, None, None,                      # amt_*
                None, None, None,                            # kdj_*
                pct if i else None, 2.0 if i else None,      # pct_chg, amplitude
                pe, pe_status, "daily_2026-08-07", "indicators_v1", "hash", now,
            ))
    conn.executemany(
        """
        INSERT INTO indicators_daily (symbol, trade_date,
            ma5, ma10, ma20, ma60, ma120, ma250,
            dif, dea, macd_hist, rsi6, rsi12, rsi24,
            boll_mid, boll_upper, boll_lower, boll_bandwidth,
            vol_ma5, vol_ma10, vol_mean20, vol_std20, vol_mean60, vol_std60,
            amt_mean20, amt_std20, amt_mean60, amt_std60,
            kdj_k, kdj_d, kdj_j, pct_chg, amplitude,
            pe_ttm, pe_status, run_id, rule_version, config_hash, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _seed_weekly(conn: sqlite3.Connection, dates: list[str]) -> None:
    # 002747.SZ 两周完成周（复权周线，因子 2.0）
    rows = [
        ("002747.SZ", "2026-07-31", "2026-07-27", 40.0, 46.0, 39.0, 45.0, 5_000_000.0, 1.0, "daily_2026-08-07"),
        ("002747.SZ", "2026-08-07", "2026-08-03", 45.0, 50.0, 44.0, 49.0, 6_000_000.0, 1.0, "daily_2026-08-07"),
    ]
    conn.executemany(
        """
        INSERT INTO weekly_bars (symbol, week_end_date, week_start_date, open_adj, high_adj,
                                 low_adj, close_adj, volume_adj, amount_raw, trading_days, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 5, ?)
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO indicators_weekly (symbol, week_end_date, ma5, ma10, ma20,
            dif, dea, macd_hist, rsi6, rsi12, rsi24,
            boll_mid, boll_upper, boll_lower, boll_bandwidth,
            vol_ma5, vol_ma10, vol_mean20, vol_std20,
            kdj_k, kdj_d, kdj_j, pct_chg, amplitude,
            run_id, rule_version, config_hash, computed_at)
        VALUES ('002747.SZ', '2026-08-07', 46.0, 45.0, 44.0,
                0.2, 0.1, 0.2, 55.0, 56.0, 57.0,
                46.0, 48.0, 44.0, 0.1,
                5000000.0, 4800000.0, 4500000.0, 300000.0,
                50.0, 50.0, 50.0, 8.9, 13.6,
                'daily_2026-08-07', 'indicators_v1', 'hash', '2026-08-10T00:00:00+00:00')
        """
    )


def _seed_signals(conn: sqlite3.Connection) -> None:
    now = "2026-08-10T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO weekly_anchors (anchor_id, symbol, as_of, anchor_type, trade_date,
                                    adjusted_price, raw_price, is_fallback, run_id, created_at)
        VALUES (1, '603605.SH', '2026-07-31', 'panic_low', '2026-07-20',
                51.0, 51.0, 0, 'weekly_signals_603605.SH', ?)
        """,
        (now,),
    )
    signals = [
        ("603605.SH", "2026-08-03", "daily_watch", "triggered", 1, 1, "2026-08-31",
         json.dumps({"tier": 1, "distance_pct": 2.0}), "daily_2026-08-07", "signals_v1", "hash", now),
        ("603605.SH", "2026-08-04", "right_side", "confirmed", 1, 1, None,
         json.dumps({"state": "confirmed"}), "daily_2026-08-07", "signals_v1", "hash", now),
        ("603605.SH", "2026-08-05", "tier_triggered", "triggered", 1, 1, "2026-08-31",
         json.dumps({"tier": 1}), "daily_2026-08-07", "signals_v1", "hash", now),
        ("603605.SH", "2026-07-10", "panic", "active", 1, 0, None,
         json.dumps({"anchor_id": 1}), "weekly_signals_603605.SH", "signals_v1", "hash", now),
        ("002747.SZ", "2026-08-06", "daily_watch", "incomplete", None, 0, None,
         json.dumps({"reason": "no_active_card"}), "daily_2026-08-07", "signals_v1", "hash", now),
        ("0700.HK", "2026-08-05", "daily_watch", "incomplete", None, 0, None,
         json.dumps({"reason": "no_active_card"}), "daily_2026-08-07", "signals_v1", "hash", now),
    ]
    conn.executemany(
        """
        INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id, triggered,
                                  active_until, details_json, run_id, rule_version, config_hash,
                                  created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        signals,
    )


def _seed_cards(conn: sqlite3.Connection) -> None:
    now = "2026-08-10T00:00:00+00:00"
    tiers = json.dumps({
        "tiers": [
            {"tier": 1, "zone_low": "55.00", "zone_high": "58.00"},
            {"tier": 2, "zone_low": "50.00", "zone_high": "54.00"},
            {"tier": 3, "zone_low": "45.00", "zone_high": "49.00"},
        ]
    })
    invalidation = json.dumps({"line": "44.00", "note": "跌破证伪"})
    swing = json.dumps({"box_low": "50.00", "box_high": "58.00",
                        "buy_zone_low": "51.00", "buy_zone_high": "53.00",
                        "sell_zone_low": "56.00", "sell_zone_high": "58.00",
                        "box_invalidation": "49.00", "position_cap_pct": 20})
    right = json.dumps({"trigger_level": "58.00", "stop_level": "53.00"})
    earnings = json.dumps({"scenarios": [{"scenario": "base", "eps": "4.00"}]})
    cards = [
        ("603605SH_120ca661", "603605.SH", "active", "card_v1", now, "2026-08-10", None,
         None, "CNY", "raw", earnings, "{}", tiers, invalidation, swing, right,
         "2026-08-31", "{}", "daily_2026-08-07"),
        ("603605SH_old01", "603605.SH", "superseded", "card_v1", now, "2026-06-01", "2026-08-09",
         None, "CNY", "raw", earnings, "{}", tiers, invalidation, swing, right,
         "2026-09-01", "{}", "daily_2026-06-01"),
        ("603605SH_draft01", "603605.SH", "draft", "card_v1", now, None, None,
         None, "CNY", "raw", earnings, "{}", tiers, invalidation, swing, right,
         None, "{}", "daily_2026-08-07"),
        ("603605SH_rej01", "603605.SH", "rejected", "card_v1", now, "2026-07-01", "2026-07-31",
         None, "CNY", "raw", earnings, "{}", tiers, invalidation, swing, right,
         None, "{}", "daily_2026-07-01"),
    ]
    conn.executemany(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status, schema_version,
            created_at, effective_from, effective_to, supersedes_id, currency, price_basis,
            earnings_scenarios_json, valuation_scenarios_json, price_tiers_json,
            invalidation_json, swing_box_json, right_side_trigger_json,
            next_review_at, input_snapshot_json, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        cards,
    )


def _seed_executions(conn: sqlite3.Connection) -> None:
    rows = [
        (1, "exec-001", "603605.SH", "2026-07-03T14:30:08+08:00", "sell", None, "60.200", "1700", "0", "603605SH_120ca661", "{}"),
        (2, "exec-002", "603605.SH", "2026-07-09T14:39:55+08:00", "buy", None, "57.000", "900", "0", "603605SH_120ca661", "{}"),
        (3, "exec-003", "603605.SH", "2026-07-13T13:44:56+08:00", "buy", "tier2", "55.930", "800", "0", "603605SH_120ca661", "{}"),
        (4, "exec-004", "603605.SH", "2026-07-15T11:15:09+08:00", "sell", None, "59.500", "1800", "0", "603605SH_120ca661", "{}"),
    ]
    conn.executemany(
        """
        INSERT INTO executions (execution_id, idempotency_key, symbol, executed_at, action_type,
                                tier, price, quantity, fees, card_version_id, signal_snapshot_json,
                                created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-08-10T00:00:00+00:00')
        """,
        rows,
    )


def _seed_runs(conn: sqlite3.Connection) -> None:
    rows = [
        ("daily_2026-08-07", "calendar", "success", "2026-08-07", "2026-08-07T08:00:00+00:00", "2026-08-07T08:00:30+00:00", None),
        ("daily_2026-08-07", "symbol:603605.SH", "success", "2026-08-07", "2026-08-07T09:00:00+00:00", "2026-08-07T09:00:10+00:00", None),
        ("daily_2026-08-07", "symbol:002747.SZ", "failed", "2026-08-07", "2026-08-07T09:01:00+00:00", "2026-08-07T09:01:05+00:00", "ingest conflict"),
        ("daily_2026-08-07", "report", "degraded", "2026-08-07", "2026-08-07T10:00:00+00:00", "2026-08-07T10:00:30+00:00", "signal degraded"),
        ("daily_2026-08-07", "summary", "success", "2026-08-07", "2026-08-07T11:00:00+00:00", "2026-08-07T11:00:10+00:00", None),
        ("daily_2026-08-06", "symbol:603605.SH", "success", "2026-08-06", "2026-08-06T09:00:00+00:00", "2026-08-06T09:00:10+00:00", None),
        ("daily_2026-08-05", "symbol:603605.SH", "success", "2026-08-05", "2026-08-05T09:00:00+00:00", "2026-08-05T09:00:10+00:00", None),
        ("daily_2026-08-04", "symbol:603605.SH", "success", "2026-08-04", "2026-08-04T09:00:00+00:00", "2026-08-04T09:00:10+00:00", None),
        ("daily_2026-08-03", "symbol:603605.SH", "degraded", "2026-08-03", "2026-08-03T09:00:00+00:00", "2026-08-03T09:00:10+00:00", "source missing"),
        ("daily_2026-08-01", "symbol:603605.SH", "success", "2026-08-01", "2026-08-01T09:00:00+00:00", "2026-08-01T09:00:10+00:00", None),
    ]
    conn.executemany(
        """
        INSERT INTO pipeline_runs (run_id, stage, status, data_cutoff, started_at, finished_at,
                                   as_of, adapter_version, config_hash, rule_version, error)
        VALUES (?, ?, ?, ?, ?, ?, '2026-08-07T00:00:00+00:00', 'v1', 'deadbeef', 'indicators_v1', ?)
        """,
        rows,
    )


def _seed_reports(conn: sqlite3.Connection) -> None:
    rows = [
        (1, "single", "603605.SH", "2026-08-07", 1, "degraded", "reports/603605.SH/2026-08-07.md"),
        (2, "daily", None, "2026-08-07", 1, "complete", "reports/daily/2026-08-07.md"),
        (3, "single", "002747.SZ", "2026-08-07", 1, "failed", None),
    ]
    conn.executemany(
        """
        INSERT INTO report_runs (report_run_id, report_type, symbol, trade_date, revision,
                                 status, file_path, as_of, rule_version, config_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '2026-08-07T00:00:00+00:00', 'report_v1', 'deadbeef',
                '2026-08-07T11:00:00+00:00')
        """,
        rows,
    )


def _seed_index_bars(conn: sqlite3.Connection) -> None:
    rows = [
        ("000300.SH", "2026-08-05", 4000.0, 4020.0, 3980.0, 4010.0, 3_000_000.0, "CNY"),
        ("000300.SH", "2026-08-06", 4010.0, 4030.0, 4000.0, 4020.0, 3_100_000.0, "CNY"),
        ("000300.SH", "2026-08-07", 4020.0, 4050.0, 4010.0, 4040.0, 3_200_000.0, "CNY"),
    ]
    conn.executemany(
        """
        INSERT INTO index_bars (index_code, trade_date, open, high, low, close, volume,
                                currency, source, available_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', '2026-08-10T00:00:00+00:00', '2026-08-10T00:00:00+00:00')
        """,
        rows,
    )


def seed_ui_data(conn: sqlite3.Connection) -> None:
    """插入全部 UI 测试种子（migrate+seed 之后调用，调用方负责 commit）。"""
    _seed_watchlist_hk(conn)
    bars = _seed_bars(conn)
    _seed_indicators(conn, bars["dates"])
    _seed_weekly(conn, bars["dates"])
    _seed_signals(conn)
    _seed_cards(conn)
    _seed_executions(conn)
    _seed_runs(conn)
    _seed_reports(conn)
    _seed_index_bars(conn)
