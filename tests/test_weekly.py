"""D1.6 周线聚合 golden tests（设计 §9.5 硬门槛：周线聚合 golden tests）。

手工小数据集（同 test_adjust 的真实价序列 p_i = 100 + 0.5*i）：
- 2026-07-22（周三）10 送 10：raw 价 ×0.495/0.99 段内倍率、raw 量 ×2；
- price_adj_factor 直接给 1/m（复权后 = 真实价，便于精确断言）；
- share_factor：送转前 0.5、后 1.0（调整后量全周一致 = 2000）。

锁定：周中除权时周高/周低不扭曲（逐日复权聚合 vs 先聚合再乘周末因子的错误口径）、
开=周首日/收=周末日/量额求和、只写完成周（进行时周不生成）、周末日无 bar 跳过、
日历缺失 incomplete 不写入。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from scripts.pipeline import db
from scripts.pipeline.weekly import rebuild_weekly

DIV_EX = "2026-07-15"
SPLIT_EX = "2026-07-22"


def _weekdays(start: str, end: str) -> list[str]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


ALL_DAYS = _weekdays("2026-07-06", "2026-07-31")


def _true_ohlc(p: float) -> tuple[float, float, float, float]:
    return p - 0.2, p + 0.7, p - 0.6, p  # o, h, l, c


def add_calendar(conn: sqlite3.Connection) -> None:
    now = db.utc_now()
    for d in _weekdays("2026-07-01", "2026-08-31"):
        conn.execute(
            """
            INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                                          session_open, session_close, status,
                                          status_detail, timezone, source, updated_at)
            VALUES ('CN', ?, 1, 1, NULL, NULL, 'trading', NULL, 'Asia/Shanghai', 'test', ?)
            """,
            (d, now),
        )
    conn.commit()


def add_bars(conn: sqlite3.Connection, days: list[str], symbol: str = "TEST.SH",
             skip: set[str] | None = None) -> None:
    """raw = true × 段内倍率；price_adj_factor = 1/倍率（复权后 = 真实价）。"""
    skip = skip or set()
    now = db.utc_now()
    for d in days:
        if d in skip:
            continue
        i = ALL_DAYS.index(d)
        p = 100.0 + 0.5 * i
        m = 1.0 if d < DIV_EX else (0.99 if d < SPLIT_EX else 0.495)
        o, h, l, c = (v * m for v in _true_ohlc(p))
        vol = 1000.0 if d < SPLIT_EX else 2000.0
        share = 0.5 if d < SPLIT_EX else 1.0
        conn.execute(
            """
            INSERT INTO daily_bars (symbol, trade_date, market,
                open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                currency, price_adj_factor, share_factor, trading_status,
                source, raw_object_id, updated_at)
            VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, NULL, 'CNY', ?, ?,
                    'normal', 'test', NULL, ?)
            """,
            (symbol, d, o, h, l, c, vol, 1.0 / m, share, now),
        )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "market.db")
    db.migrate(c)
    add_calendar(c)
    yield c
    c.close()


def _weeks(conn, symbol="TEST.SH"):
    return {r["week_end_date"]: r for r in conn.execute(
        "SELECT * FROM weekly_bars WHERE symbol = ?", (symbol,))}


def test_weekly_aggregation_full_month(conn):
    add_bars(conn, ALL_DAYS)
    with conn:
        res = rebuild_weekly(conn, "TEST.SH", run_id="run_w1")
    assert res.weeks_written == 4
    assert res.last_week_end == "2026-07-31"
    assert res.skipped_in_progress == []
    weeks = _weeks(conn)
    assert sorted(weeks) == ["2026-07-10", "2026-07-17", "2026-07-24", "2026-07-31"]

    w1 = weeks["2026-07-10"]  # i=0..4, p=100..102
    assert w1["week_start_date"] == "2026-07-06"
    assert w1["open_adj"] == pytest.approx(99.8)
    assert w1["close_adj"] == pytest.approx(102.0)
    assert w1["high_adj"] == pytest.approx(102.7)
    assert w1["low_adj"] == pytest.approx(99.4)
    assert w1["volume_adj"] == pytest.approx(5 * 2000.0)  # raw1000/0.5 与 raw2000/1.0 一致
    assert w1["amount_raw"] is None  # 来源无成交额 → 求和为 NULL
    assert w1["trading_days"] == 5


def test_midweek_split_no_distortion(conn):
    """周中 10 送 10：逐日复权聚合 → 周高/周低连续不扭曲。

    错误口径（先聚合不复权再乘周末因子 1/0.495≈2.02）会把周一高点放大到
    ≈211；正确口径下全周复权价落在 104.4~107.7。
    """
    add_bars(conn, ALL_DAYS)
    with conn:
        rebuild_weekly(conn, "TEST.SH")
    w3 = _weeks(conn)["2026-07-24"]  # i=10..14, p=105..107
    assert w3["open_adj"] == pytest.approx(104.8)   # 周一真实开盘价
    assert w3["close_adj"] == pytest.approx(107.0)  # 周五真实收盘价
    assert w3["high_adj"] == pytest.approx(107.7)
    assert w3["low_adj"] == pytest.approx(104.4)
    assert w3["high_adj"] < 120  # 未被周末因子扭曲放大
    assert w3["volume_adj"] == pytest.approx(5 * 2000.0)


def test_in_progress_week_not_written(conn):
    """数据截至周四 07-30：进行时周（周末 07-31 未过）不写入。"""
    add_bars(conn, [d for d in ALL_DAYS if d <= "2026-07-30"])
    with conn:
        res = rebuild_weekly(conn, "TEST.SH")
    assert res.weeks_written == 3
    assert res.last_week_end == "2026-07-24"
    assert res.skipped_in_progress == ["2026-07-31"]
    assert "2026-07-31" not in _weeks(conn)


def test_week_end_without_bar_skipped(conn):
    """周末交易日无 bar（停牌）→ 该周跳过不写入。"""
    add_bars(conn, ALL_DAYS, skip={"2026-07-17"})
    with conn:
        res = rebuild_weekly(conn, "TEST.SH")
    assert res.weeks_written == 3
    assert "2026-07-17" not in _weeks(conn)
    assert any("周末交易日无 bar" in n for n in res.notes)


def test_calendar_missing_writes_nothing(tmp_path):
    """日历缺失：incomplete，不猜不写（§2.5）。"""
    c = db.connect(tmp_path / "market.db")
    db.migrate(c)
    add_bars(c, ALL_DAYS)  # 不建日历
    with c:
        res = rebuild_weekly(c, "TEST.SH")
    assert res.weeks_written == 0
    assert any("incomplete" in n for n in res.notes)
    assert _weeks(c) == {}
    c.close()


def test_rebuild_is_idempotent(conn):
    """派生数据整 symbol 删除后重算：重跑结果一致，无重复行。"""
    add_bars(conn, ALL_DAYS)
    with conn:
        rebuild_weekly(conn, "TEST.SH", run_id="run_a")
    first = _weeks(conn)
    with conn:
        rebuild_weekly(conn, "TEST.SH", run_id="run_b")
    second = _weeks(conn)
    assert len(first) == len(second) == 4
    for k in first:
        assert first[k]["close_adj"] == pytest.approx(second[k]["close_adj"])
