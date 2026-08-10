"""SQLite 数据库层：连接、migration、种子导入（设计 §7）。

CLI：
    uv run python -m scripts.pipeline.db migrate
    uv run python -m scripts.pipeline.db seed      # 导入 watchlist 与日历种子（自动先 migrate）

数据库文件默认 data/market.db，可用 --db 覆盖。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "data" / "market.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
CONFIG_DIR = ROOT / "config"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """打开连接：开启外键约束与 WAL 模式（设计 §7）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ---------------------------------------------------------------- migration

def migrate(conn: sqlite3.Connection) -> list[str]:
    """按序应用 scripts/pipeline/migrations/*.sql；已应用的跳过，可重复执行。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    newly: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_")[0])
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        record = (
            f"INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({version}, '{path.name}', '{utc_now()}');"
        )
        # 单事务执行整个 migration，失败整体回滚
        conn.executescript(f"BEGIN;\n{sql}\n{record}\nCOMMIT;")
        newly.append(path.name)
    return newly


# ---------------------------------------------------------------- seed

def seed_watchlist(conn: sqlite3.Connection, path: Path | None = None) -> int:
    """导入 config/watchlist.yaml（配置表，允许 upsert）。"""
    path = path or CONFIG_DIR / "watchlist.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    now = utc_now()
    for s in doc["stocks"]:
        conn.execute(
            """
            INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,
                                   currency, timezone, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                market=excluded.market, name=excluded.name,
                aliases_json=excluded.aliases_json,
                benchmark_code=excluded.benchmark_code,
                currency=excluded.currency, timezone=excluded.timezone,
                active=1, updated_at=excluded.updated_at
            """,
            (
                s["symbol"], s["market"], s["name"],
                json.dumps(s.get("aliases") or [], ensure_ascii=False),
                s["benchmark_code"], s["currency"], s["timezone"], now, now,
            ),
        )
    return len(doc["stocks"])


def seed_calendar(conn: sqlite3.Connection, path: Path) -> int:
    """把 config/calendar_{market}_{year}.yaml 展开为 trading_calendar 逐日行。

    规则：周一至周五开市，周末休市，holidays 区间休市，half_days 为半日市。
    种子文件带 status: incomplete_todo 时跳过（设计 §2.5：未填充前校验输出 incomplete）。
    返回导入行数；跳过返回 0。
    """
    path = Path(path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if doc.get("status") == "incomplete_todo":
        print(f"[seed] 跳过 {path.name}：status=incomplete_todo（日历未填充，"
              f"该市场标的在日历校验中输出 incomplete）")
        return 0

    market = doc["market"]
    year = int(doc["year"])
    tz = doc["timezone"]
    session = doc.get("session") or {}

    # 休市区间与半日市展开为 {date: name}
    def expand(entries, key_from="from"):
        out: dict[date, str] = {}
        for e in entries or []:
            if key_from in e:  # {name, from, to}
                d = e["from"]
                while d <= e["to"]:
                    out[d] = e["name"]
                    d += timedelta(days=1)
            else:  # {name, date}
                out[e["date"]] = e["name"]
        return out

    holidays = expand(doc.get("holidays"))
    half_days = expand(doc.get("half_days"))

    start = date(year, 1, 1)
    conn.execute(
        "DELETE FROM trading_calendar WHERE market = ? AND trade_date BETWEEN ? AND ?",
        (market, f"{year}-01-01", f"{year}-12-31"),
    )
    now = utc_now()
    rows = []
    d = start
    while d.year == year:
        ds = d.isoformat()
        if d in holidays:
            rows.append((market, ds, 0, 0, None, None, "holiday", holidays[d], tz, path.name, now))
        elif d.weekday() >= 5:
            rows.append((market, ds, 0, 0, None, None, "weekend", None, tz, path.name, now))
        elif d in half_days:
            rows.append((market, ds, 1, 0, session.get("open"), session.get("close"),
                         "half_day", half_days[d], tz, path.name, now))
        else:
            full = 1 if session.get("full_day", True) else 0
            rows.append((market, ds, 1, full, session.get("open"), session.get("close"),
                         "trading", None, tz, path.name, now))
        d += timedelta(days=1)
    conn.executemany(
        """
        INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                                      session_open, session_close, status, status_detail,
                                      timezone, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def seed(conn: sqlite3.Connection) -> None:
    """导入全部种子：watchlist + config/calendar_*.yaml（先确保 schema 就绪）。"""
    migrate(conn)
    n = seed_watchlist(conn)
    print(f"[seed] watchlist 导入 {n} 只股票")
    for path in sorted(CONFIG_DIR.glob("calendar_*.yaml")):
        n = seed_calendar(conn, path)
        if n:
            print(f"[seed] {path.name} 导入 {n} 天")
    conn.commit()


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.db")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="数据库文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="应用未执行的 migration")
    sub.add_parser("seed", help="导入 watchlist 与交易日历种子（先自动 migrate）")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        applied = migrate(conn)
        if applied:
            print(f"[migrate] 应用 {len(applied)} 个 migration：{', '.join(applied)}")
        else:
            print("[migrate] 无待应用的 migration（已是最新）")
        if args.cmd == "seed":
            seed(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
