"""D1.4 交易日历校验门禁测试（手工 fixture，不依赖网络）。

覆盖：有行情 / 停牌 / 来源缺数 / 非交易日 四分支，日历缺失 incomplete，
指数行情与日历交叉校验的两个冲突方向。
"""

from __future__ import annotations

import pytest

from scripts.pipeline import db
from scripts.pipeline.calendar_check import (
    STATUS_INCOMPLETE,
    STATUS_NON_TRADING,
    STATUS_OK,
    STATUS_SOURCE_MISSING,
    STATUS_SUSPENDED,
    check_symbol_day,
    cross_check_index_calendar,
)

CN_CAL = {
    "2026-08-05": (1, "trading"),
    "2026-08-06": (1, "trading"),
    "2026-08-07": (1, "trading"),
    "2026-08-08": (0, "weekend"),
}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "market.db")
    db.migrate(c)
    now = db.utc_now()
    for d, (is_open, status) in CN_CAL.items():
        c.execute(
            """
            INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                                          session_open, session_close, status,
                                          status_detail, timezone, source, updated_at)
            VALUES ('CN', ?, ?, 1, NULL, NULL, ?, NULL, 'Asia/Shanghai', 'test', ?)
            """,
            (d, is_open, status, now),
        )
    c.commit()
    yield c
    c.close()


def add_bar(conn, symbol, trade_date, market="CN"):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
                                low_raw, close_raw, volume_raw, amount_raw, currency,
                                price_adj_factor, share_factor, trading_status,
                                source, raw_object_id, updated_at)
        VALUES (?, ?, ?, 1, 1, 1, 1, 1, NULL, 'CNY', 1.0, 1.0, 'normal',
                'test', NULL, '2026-08-09T00:00:00+00:00')
        """,
        (symbol, trade_date, market),
    )
    conn.commit()


def add_index_bar(conn, index_code, trade_date):
    conn.execute(
        """
        INSERT INTO index_bars (index_code, trade_date, open, high, low, close,
                                volume, currency, source, available_at,
                                raw_object_id, updated_at)
        VALUES (?, ?, 1, 1, 1, 1, 1, 'CNY', 'test', NULL, NULL,
                '2026-08-09T00:00:00+00:00')
        """,
        (index_code, trade_date),
    )
    conn.commit()


def test_trading_with_bars(conn):
    add_bar(conn, "603605.SH", "2026-08-06")
    check = check_symbol_day(conn, "603605.SH", "2026-08-06")
    assert check.status == STATUS_OK


def test_suspended(conn):
    """交易日个股无 bar 但基准指数有 bar → 停牌。"""
    add_index_bar(conn, "000300.SH", "2026-08-06")
    check = check_symbol_day(conn, "603605.SH", "2026-08-06")
    assert check.status == STATUS_SUSPENDED
    assert "停牌" in check.reason


def test_source_missing(conn):
    """交易日个股与指数都无 bar → 来源缺数（不误判为停牌或非交易日）。"""
    check = check_symbol_day(conn, "603605.SH", "2026-08-05")
    assert check.status == STATUS_SOURCE_MISSING
    assert "缺数" in check.reason


def test_non_trading_day(conn):
    check = check_symbol_day(conn, "603605.SH", "2026-08-08")
    assert check.status == STATUS_NON_TRADING


def test_incomplete_when_calendar_missing(conn):
    """HK 日历未种子化（incomplete_todo）→ incomplete，不猜（§2.5）。"""
    check = check_symbol_day(conn, "0700.HK", "2026-08-06")
    assert check.status == STATUS_INCOMPLETE
    assert "trading_calendar 缺失" in check.reason


def test_incomplete_when_date_out_of_seed_range(conn):
    check = check_symbol_day(conn, "603605.SH", "2027-01-05")
    assert check.status == STATUS_INCOMPLETE
    assert "种子范围" in check.reason


def test_cross_check_clean(conn):
    add_index_bar(conn, "000300.SH", "2026-08-05")
    add_index_bar(conn, "000300.SH", "2026-08-06")
    add_index_bar(conn, "000300.SH", "2026-08-07")
    res = cross_check_index_calendar(conn, "CN", "2026-08-05", "2026-08-07")
    assert res.status == "ok"
    assert res.conflicts == []
    assert res.checked_days == 3


def test_cross_check_index_bar_on_closed_day(conn):
    """指数有 bar 但日历休市 → 冲突。"""
    add_index_bar(conn, "000300.SH", "2026-08-08")  # 周六
    res = cross_check_index_calendar(conn, "CN", "2026-08-08", "2026-08-08")
    assert res.status == "conflict"
    assert any("休市" in c and "有 bar" in c for c in res.conflicts)


def test_cross_check_open_day_without_index_bar(conn):
    """日历开市但指数无 bar → 冲突（来源缺数或日历错误）。"""
    res = cross_check_index_calendar(conn, "CN", "2026-08-05", "2026-08-05")
    assert res.status == "conflict"
    assert any("开市" in c and "无 bar" in c for c in res.conflicts)


def test_cross_check_calendar_missing(conn):
    res = cross_check_index_calendar(conn, "HK", "2026-08-05", "2026-08-07")
    assert res.status == "conflict"
    assert any("incomplete" in c for c in res.conflicts)
