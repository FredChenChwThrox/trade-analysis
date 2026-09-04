"""D1.2 数据库层测试：migration 幂等、外键/WAL、watchlist 与日历种子。"""

import pytest

from scripts.pipeline import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "market.db")
    yield c
    c.close()


def test_migrate_idempotent(conn):
    first = db.migrate(conn)
    assert first == [
        "0001_init.sql",
        "0002_event_assessments_symbol_weekly_anchors_identity.sql",
        "0003_message_calendar.sql",
        "0004_macro_factors.sql",
        "0005_llm_eval.sql",
        "0006_symbol_names.sql",
        "0007_financial_statements.sql",
        "0008_daily_bars_turnover.sql",
        "0009_holder_stats.sql",
        "0010_chip_distribution.sql",
    ]
    second = db.migrate(conn)  # 第二遍：已应用的跳过，不报错
    assert second == []
    rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [r["version"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_foreign_keys_and_wal_enabled(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_foreign_keys_enforced(conn):
    db.migrate(conn)
    with pytest.raises(Exception):
        # event_symbols 引用不存在的 events 行，必须被外键拒绝
        conn.execute(
            "INSERT INTO event_symbols (event_id, symbol) VALUES ('no_such_event', '603605.SH')"
        )


def test_watchlist_seed(conn):
    db.seed(conn)
    row = conn.execute(
        "SELECT symbol, market, name, benchmark_code, currency, timezone "
        "FROM watchlist WHERE symbol = '603605.SH'"
    ).fetchone()
    assert row is not None
    assert row["market"] == "CN"
    assert row["name"] == "珀莱雅"
    assert row["benchmark_code"] == "000300.SH"


def test_calendar_seed(conn):
    db.seed(conn)

    def day(d):
        return conn.execute(
            "SELECT is_open, status, status_detail FROM trading_calendar "
            "WHERE market = 'CN' AND trade_date = ?",
            (d,),
        ).fetchone()

    spring = day("2026-02-16")  # 春节假期内（周一）
    assert spring["is_open"] == 0
    assert spring["status"] == "holiday"
    assert spring["status_detail"] == "春节"

    monday = day("2026-08-10")  # 普通周一
    assert monday["is_open"] == 1
    assert monday["status"] == "trading"

    saturday = day("2026-08-15")  # 周六
    assert saturday["is_open"] == 0
    assert saturday["status"] == "weekend"


def test_calendar_seed_skips_incomplete_hk(conn, capsys):
    db.seed(conn)
    out = capsys.readouterr().out
    assert "incomplete_todo" in out  # HK 种子被跳过并打印提示
    assert conn.execute(
        "SELECT COUNT(*) FROM trading_calendar WHERE market = 'HK'"
    ).fetchone()[0] == 0


def test_calendar_seed_idempotent(conn):
    db.seed(conn)
    db.seed(conn)  # 重复导入不重行
    # CN 日历种子覆盖 2023–2026 四年（365+366+365+365）
    assert conn.execute(
        "SELECT COUNT(*) FROM trading_calendar WHERE market = 'CN'"
    ).fetchone()[0] == 365 + 366 + 365 + 365
