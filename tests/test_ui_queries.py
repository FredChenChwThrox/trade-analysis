"""任务 01：数据查询层 tests/test_ui_queries.py。"""

import json

import pytest

from scripts.ui import queries


def _bar_by_date(bars, d):
    return next(b for b in bars if b["trade_date"] == d)


def test_list_markets(ui_conn):
    assert queries.list_markets(ui_conn) == ["CN", "HK"]


def test_list_run_ids(ui_conn):
    ids = queries.list_run_ids(ui_conn, limit=10)
    assert ids[0]["run_id"] == "daily_2026-08-07"
    assert "last_run_at" in ids[0]


def test_list_card_status(ui_conn):
    assert sorted(queries.list_card_status(ui_conn)) == ["active", "draft", "rejected", "superseded"]


# ---------------------------------------------------------------- list_stocks

def test_list_stocks_happy_path(ui_conn):
    data = queries.list_stocks(ui_conn, {}, page=1, page_size=50)
    assert data["total"] == 8
    assert data["page"] == 1
    by_symbol = {it["symbol"]: it for it in data["items"]}

    row = by_symbol["603605.SH"]
    assert row["name"] == "珀莱雅"
    assert row["market"] == "CN"
    assert row["latest_trade_date"] == "2026-08-07"
    assert row["latest_close"] == 79.0
    assert row["price_adj_factor"] == 1.0
    assert row["pe_ttm"] == 15.0
    assert row["pct_chg"] == -0.5
    assert row["active_card_id"] == "603605SH_120ca661"
    assert row["signal_count_5d"] == 3
    assert row["last_run_status"] == "success"

    row = by_symbol["002747.SZ"]
    assert row["latest_close"] == 49.0
    assert row["pe_ttm"] == 25.0
    assert row["active_card_id"] is None
    assert row["signal_count_5d"] == 0
    assert row["last_run_status"] == "failed"


def test_list_stocks_filter_market(ui_conn):
    data = queries.list_stocks(ui_conn, {"market": ["HK"]})
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "0700.HK"


def test_list_stocks_filter_search(ui_conn):
    by_name = queries.list_stocks(ui_conn, {"q": "珀莱雅"})
    assert [it["symbol"] for it in by_name["items"]] == ["603605.SH"]
    by_symbol = queries.list_stocks(ui_conn, {"q": "002747"})
    assert [it["symbol"] for it in by_symbol["items"]] == ["002747.SZ"]
    by_alias = queries.list_stocks(ui_conn, {"q": "Proya"})
    assert [it["symbol"] for it in by_alias["items"]] == ["603605.SH"]


def test_list_stocks_filter_has_active_card(ui_conn):
    data = queries.list_stocks(ui_conn, {"has_active_card": True})
    assert [it["symbol"] for it in data["items"]] == ["603605.SH"]


def test_list_stocks_filter_pe_range(ui_conn):
    data = queries.list_stocks(ui_conn, {"pe_min": 20, "pe_max": 30})
    assert [it["symbol"] for it in data["items"]] == ["002747.SZ"]


def test_list_stocks_filter_pct_chg_range(ui_conn):
    data = queries.list_stocks(ui_conn, {"pct_chg_min": 1.0})
    assert [it["symbol"] for it in data["items"]] == ["002747.SZ"]


def test_list_stocks_filter_volume_min(ui_conn):
    data = queries.list_stocks(ui_conn, {"volume_min": 1_028_000})  # 002747 last volume=1,029,000
    by_symbol = {it["symbol"]: it for it in data["items"]}
    assert "002747.SZ" in by_symbol
    assert "603605.SH" in by_symbol  # last volume = 1,029,000


def test_list_stocks_filter_data_quality_pe_status(ui_conn):
    data = queries.list_stocks(ui_conn, {"pe_status": ["no_share_capital"]})
    assert [it["symbol"] for it in data["items"]] == ["0700.HK"]
    # 虚拟码：ok / degraded / missing
    ok = queries.list_stocks(ui_conn, {"pe_status": ["ok"]})
    assert {it["symbol"] for it in ok["items"]} == {"603605.SH", "002747.SZ"}
    missing = queries.list_stocks(ui_conn, {"pe_status": ["missing"]})
    assert [it["symbol"] for it in missing["items"]] == ["0700.HK"]
    degraded = queries.list_stocks(ui_conn, {"pe_status": ["degraded"]})
    assert degraded["total"] == 0


def test_list_stocks_filter_recent_signal_days(ui_conn):
    data = queries.list_stocks(ui_conn, {"recent_signal_days": 5})
    assert [it["symbol"] for it in data["items"]] == ["603605.SH"]


def test_list_stocks_sort_and_pagination(ui_conn):
    data = queries.list_stocks(ui_conn, {}, page=1, page_size=2, sort="latest_close", order="desc")
    assert data["page_size"] == 2
    assert data["total"] == 8
    assert data["items"][0]["symbol"] == "0700.HK"  # close 314 > 79
    asc = queries.list_stocks(ui_conn, {}, page=1, page_size=2, sort="pe_ttm", order="asc")
    # 0700.HK pe 为 NULL，升序时 NULL 排前
    assert asc["items"][0]["symbol"] == "0700.HK"


# ---------------------------------------------------------------- single stock

def test_get_stock_meta(ui_conn):
    meta = queries.get_stock_meta(ui_conn, "603605.SH")
    assert meta["symbol"] == "603605.SH"
    assert meta["name"] == "珀莱雅"
    assert meta["latest"]["trade_date"] == "2026-08-07"
    assert meta["latest"]["close_raw"] == 79.0
    assert meta["indicators"]["pe_ttm"] == 15.0
    assert meta["active_card"]["card_version_id"] == "603605SH_120ca661"
    assert meta["tier_state"]["tier"] is None  # close 79 超出三档价区（最高 58）
    assert meta["tier_state"]["nearest"] == 58.0
    assert len(meta["recent_runs"]) >= 4


def test_get_stock_meta_unknown(ui_conn):
    assert queries.get_stock_meta(ui_conn, "NOPE.X") is None


def test_get_stock_bars_daily_unadjusted(ui_conn):
    bars = queries.get_stock_bars(ui_conn, "002747.SZ", "daily", None, None, "unadjusted")
    assert len(bars) == 30
    assert bars[0]["trade_date"] == "2026-06-29"
    assert bars[0]["close"] == 20.0
    last = _bar_by_date(bars, "2026-08-07")
    assert last["close"] == 49.0
    assert last["open"] == 48.5


def test_get_stock_bars_daily_fully_adjusted(ui_conn):
    bars = queries.get_stock_bars(ui_conn, "002747.SZ", "daily", None, None, "fully_adjusted")
    last = _bar_by_date(bars, "2026-08-07")
    assert last["close"] == 49.0 * 2.0
    assert last["open"] == 48.5 * 2.0
    assert last["volume"] == 1_029_000  # share_factor=1.0 不变


def test_get_stock_bars_daily_date_range(ui_conn):
    bars = queries.get_stock_bars(ui_conn, "603605.SH", "daily", "2026-08-03", "2026-08-05")
    assert [b["trade_date"] for b in bars] == ["2026-08-03", "2026-08-04", "2026-08-05"]


def test_get_stock_bars_weekly_fully_adjusted(ui_conn):
    bars = queries.get_stock_bars(ui_conn, "002747.SZ", "weekly", None, None, "fully_adjusted")
    week = _bar_by_date(bars, "2026-08-07")
    assert week["open"] == 45.0
    assert week["high"] == 50.0
    assert week["low"] == 44.0
    assert week["close"] == 49.0
    assert week["volume"] == 6_000_000


def test_get_stock_bars_weekly_unadjusted_aggregates_raw(ui_conn):
    bars = queries.get_stock_bars(ui_conn, "002747.SZ", "weekly", None, None, "unadjusted")
    week = _bar_by_date(bars, "2026-08-07")
    # 本周(08-03..08-07)不复权聚合：open=首日open_raw=44.5, close=末日close_raw=49
    assert week["open"] == 44.5
    assert week["close"] == 49.0
    assert week["high"] == 50.0  # 45..49 各 +1
    assert week["low"] == 44.0
    assert week["volume"] == sum(1_000_000 + i * 1000 for i in range(25, 30))


# ---------------------------------------------------------------- indicators

def test_get_stock_indicators_all_fields(ui_conn):
    rows = queries.get_stock_indicators(ui_conn, "603605.SH", "daily", None, None)
    assert len(rows) == 30
    assert "ma5" in rows[0]
    assert "pe_ttm" in rows[0]
    assert "date" in rows[0]


def test_get_stock_indicators_fields_subset(ui_conn):
    rows = queries.get_stock_indicators(ui_conn, "603605.SH", "daily", None, None,
                                        fields=["ma5", "pe_ttm"])
    assert set(rows[0].keys()) == {"date", "ma5", "pe_ttm"}


def test_get_stock_indicators_invalid_field(ui_conn):
    with pytest.raises(ValueError):
        queries.get_stock_indicators(ui_conn, "603605.SH", "daily", None, None,
                                     fields=["ma5", "not_a_column"])


def test_get_stock_indicators_unadjusted_back_converts_ma(ui_conn):
    # 002747.SZ 因子 2.0：存储 ma5=94.0，折回后应等于 47.0
    rows = queries.get_stock_indicators(ui_conn, "002747.SZ", "daily", None, None,
                                        fields=["ma5"], price="unadjusted")
    last = rows[-1]
    assert last["date"] == "2026-08-07"
    assert last["ma5"] == 47.0
    back = queries.get_stock_indicators(ui_conn, "002747.SZ", "daily", None, None,
                                        fields=["ma5"], price="adjusted_back")
    assert back[-1]["ma5"] == 47.0


def test_get_stock_indicators_fully_adjusted_keeps_ma(ui_conn):
    rows = queries.get_stock_indicators(ui_conn, "002747.SZ", "daily", None, None,
                                        fields=["ma5"], price="fully_adjusted")
    assert rows[-1]["ma5"] == 94.0


def test_get_stock_indicators_weekly(ui_conn):
    # 默认 price=unadjusted：周线存储 ma5=46.0（复权），因子 2.0 → 折回 23.0
    rows = queries.get_stock_indicators(ui_conn, "002747.SZ", "weekly", None, None,
                                        fields=["ma5", "rsi12"])
    assert rows[-1]["date"] == "2026-08-07"
    assert rows[-1]["ma5"] == 23.0
    adj = queries.get_stock_indicators(ui_conn, "002747.SZ", "weekly", None, None,
                                       fields=["ma5"], price="fully_adjusted")
    assert adj[-1]["ma5"] == 46.0


# ---------------------------------------------------------------- signals

def test_list_signals_basic(ui_conn):
    data = queries.list_signals(ui_conn, {})
    assert data["total"] == 6
    assert data["items"][0]["fact_id"]  # 有 fact_id


def test_list_signals_filter_symbols(ui_conn):
    data = queries.list_signals(ui_conn, {"symbols": ["603605.SH"]})
    assert data["total"] == 4
    assert all(it["symbol"] == "603605.SH" for it in data["items"])


def test_list_signals_filter_signals_and_triggered(ui_conn):
    data = queries.list_signals(ui_conn, {"signals": ["daily_watch"], "triggered": True})
    assert data["total"] == 1
    assert data["items"][0]["observed_on"] == "2026-08-03"


def test_list_signals_filter_states(ui_conn):
    data = queries.list_signals(ui_conn, {"states": ["incomplete"]})
    assert data["total"] == 2


def test_list_signals_date_boundary(ui_conn):
    data = queries.list_signals(ui_conn, {"start": "2026-08-04"})
    dates = sorted(it["observed_on"] for it in data["items"])
    assert min(dates) >= "2026-08-04"
    assert "2026-08-03" not in dates


def test_list_signals_sort_desc_default(ui_conn):
    data = queries.list_signals(ui_conn, {"symbols": ["603605.SH"]}, sort="observed_on", order="desc")
    assert data["items"][0]["observed_on"] == "2026-08-05"


def test_get_signal_details_with_anchor(ui_conn):
    # 找到 panic 信号
    data = queries.list_signals(ui_conn, {"signals": ["panic"]})
    fact_id = data["items"][0]["fact_id"]
    detail = queries.get_signal_details(ui_conn, fact_id)
    assert detail["symbol"] == "603605.SH"
    assert detail["anchor"]["anchor_type"] == "panic_low"
    assert detail["anchor"]["trade_date"] == "2026-07-20"


def test_get_signal_details_unknown(ui_conn):
    assert queries.get_signal_details(ui_conn, 999999) is None


# ---------------------------------------------------------------- cards

def test_list_cards(ui_conn):
    data = queries.list_cards(ui_conn, {})
    assert data["total"] == 4
    active = next(it for it in data["items"] if it["status"] == "active")
    assert active["card_version_id"] == "603605SH_120ca661"
    assert active["tier_summary"] == [(1, "55.00", "58.00"), (2, "50.00", "54.00"), (3, "45.00", "49.00")]


def test_list_cards_filter_status(ui_conn):
    data = queries.list_cards(ui_conn, {"status": ["active"]})
    assert data["total"] == 1


def test_list_cards_filter_effective_range(ui_conn):
    data = queries.list_cards(ui_conn, {"effective_from": "2026-07-01", "effective_to": "2026-07-31"})
    # 生效区间与 [07-01, 07-31] 重叠的卡片（old01: 06-01~08-09、rej01: 07-01~07-31）
    assert data["total"] == 2
    ids = {it["card_version_id"] for it in data["items"]}
    assert ids == {"603605SH_old01", "603605SH_rej01"}


def test_get_card_detail(ui_conn):
    detail = queries.get_card_detail(ui_conn, "603605SH_120ca661")
    assert detail["price_tiers_json"]["tiers"][0]["zone_low"] == "55.00"
    assert detail["invalidation_json"]["line"] == "44.00"
    assert detail["swing_box_json"]["box_high"] == "58.00"
    assert detail["right_side_trigger_json"]["trigger_level"] == "58.00"
    assert detail["effective_from"] == "2026-08-10"


def test_get_card_detail_unknown(ui_conn):
    assert queries.get_card_detail(ui_conn, "NOPE") is None


# ---------------------------------------------------------------- executions

def test_list_executions_by_symbol(ui_conn):
    data = queries.list_executions(ui_conn, symbol="603605.SH")
    assert data["total"] == 4
    assert data["items"][0]["action_type"] in {"buy", "sell"}


def test_list_executions_date_range(ui_conn):
    data = queries.list_executions(ui_conn, start="2026-07-10", end="2026-07-14")
    assert data["total"] == 1  # 仅 07-13 落入 [07-10, 07-15) 区间
    assert "07-13" in data["items"][0]["executed_at"]


# ---------------------------------------------------------------- runs

def test_list_pipeline_runs(ui_conn):
    data = queries.list_pipeline_runs(ui_conn, {})
    assert data["total"] == 10
    first = data["items"][0]
    assert first["run_id"] == "daily_2026-08-07"
    assert "duration_sec" in first


def test_list_pipeline_runs_filters(ui_conn):
    data = queries.list_pipeline_runs(ui_conn, {"status": ["failed"]})
    assert data["total"] == 1
    assert data["items"][0]["error"] == "ingest conflict"
    stage = queries.list_pipeline_runs(ui_conn, {"stage": "symbol:603605.SH"})
    assert stage["total"] == 6
    run_id = queries.list_pipeline_runs(ui_conn, {"run_id": "daily_2026-08-06"})
    assert run_id["total"] == 1


def test_list_report_runs(ui_conn):
    data = queries.list_report_runs(ui_conn, {})
    assert data["total"] == 3
    assert data["items"][0]["report_type"] == "single"


def test_list_report_runs_filters(ui_conn):
    data = queries.list_report_runs(ui_conn, {"report_type": "daily"})
    assert data["total"] == 1
    status = queries.list_report_runs(ui_conn, {"status": "complete"})
    assert status["total"] == 1
    sym = queries.list_report_runs(ui_conn, {"symbol": "603605.SH"})
    assert sym["total"] == 1


# ---------------------------------------------------------------- auxiliary

def test_get_watchlist(ui_conn):
    items = queries.get_watchlist(ui_conn)
    assert len(items) == 8
    assert {it["symbol"] for it in items} >= {"603605.SH", "0700.HK"}


def test_get_trading_dates(ui_conn):
    dates = queries.get_trading_dates(ui_conn, "CN", "2026-08-03", "2026-08-07")
    assert dates == ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]


def test_get_latest_trade_date(ui_conn):
    d = queries.get_latest_trade_date(ui_conn, "CN")
    assert d > "2026-08-07"


def test_get_benchmark_bars(ui_conn):
    bars = queries.get_benchmark_bars(ui_conn, "000300.SH", "2026-08-01", "2026-08-07")
    assert bars and bars[-1]["trade_date"] == "2026-08-07"


def test_search_stocks(ui_conn):
    items = queries.search_stocks(ui_conn, "平安")
    assert items and items[0]["symbol"] == "601318.SH"
