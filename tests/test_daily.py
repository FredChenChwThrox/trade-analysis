"""D1.8 每日 pipeline 测试（设计 §8.1、§8.3、§2.5）。

锁定：
- 完整 pipeline 在临时库跑通（入库→门禁→周线→指标→pipeline_runs 阶段记录）；
- 非交易日输出 non_trading_day 并跳过计算，不报错；
- 同一 date 幂等重跑：raw content hash 去重、指标 DELETE+重插结果一致、
  pipeline_runs 同 run_id 覆盖不膨胀；
- 单股入库校验失败 → 该股当日全部阶段回滚标记 failed，不影响其他股票；
- 日历缺失市场输出 incomplete（§2.5 不猜）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.pipeline import db
from scripts.pipeline.daily import (
    ST_FAILED,
    ST_INCOMPLETE,
    ST_NON_TRADING,
    ST_OK,
    main,
    run_daily,
    symbol_status,
)
from scripts.signals import event_study as es_mod

RUN_DATE = "2026-08-07"  # 周五
WEEK = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]

_PRICE_HEADER = "open,high,low,close,volume,thscode,time,thsname_cn,thsname_en,currency\n"


def _price_csv(symbol: str, day: str, close: float, *, bad: bool = False) -> str:
    o, h, l = close - 0.3, close + 0.5, close - 0.6
    if bad:
        h, l = l, h  # 违反 low<=open/close<=high → 整批校验冲突
    d = day.replace("-", "")
    return (_PRICE_HEADER +
            f"{o},{h},{l},{close},10000,{symbol},{d},测试,TEST,CNY\n")


def _add_calendar(conn: sqlite3.Connection, market: str = "CN") -> None:
    now = db.utc_now()
    d, end = date(2026, 8, 1), date(2026, 8, 31)
    while d <= end:
        is_open = 1 if d.weekday() < 5 else 0
        status = "trading" if is_open else "weekend"
        conn.execute(
            """
            INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                session_open, session_close, status, status_detail, timezone,
                source, updated_at)
            VALUES (?, ?, ?, 1, NULL, NULL, ?, NULL, 'Asia/Shanghai', 'test', ?)
            """,
            (market, d.isoformat(), is_open, status, now),
        )
        d += timedelta(days=1)
    conn.commit()


def _add_watchlist(conn: sqlite3.Connection, symbol: str, market: str = "CN") -> None:
    now = db.utc_now()
    conn.execute(
        """
        INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,
                               currency, timezone, active, created_at, updated_at)
        VALUES (?, ?, '测试', '[]', '000300.SH', 'CNY', 'Asia/Shanghai', 1, ?, ?)
        """,
        (symbol, market, now, now),
    )
    conn.commit()


def _add_bars(conn: sqlite3.Connection, symbol: str, days: list[str]) -> None:
    now = db.utc_now()
    for i, d in enumerate(days):
        p = 100.0 + i
        conn.execute(
            """
            INSERT INTO daily_bars (symbol, trade_date, market,
                open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                currency, price_adj_factor, share_factor, trading_status,
                source, raw_object_id, updated_at)
            VALUES (?, ?, 'CN', ?, ?, ?, ?, 10000, NULL, 'CNY', 1.0, 1.0,
                    'normal', 'test', NULL, ?)
            """,
            (symbol, d, p - 0.3, p + 0.5, p - 0.6, p, now),
        )
    conn.commit()


def _add_index_bars(conn: sqlite3.Connection, days: list[str]) -> None:
    now = db.utc_now()
    for i, d in enumerate(days):
        c = 4005.0 + i
        conn.execute(
            """
            INSERT INTO index_bars (index_code, trade_date, open, high, low,
                close, volume, currency, source, updated_at)
            VALUES ('000300.SH', ?, ?, ?, ?, ?, 1e8, 'CNY', 'test', ?)
            """,
            (d, c - 5, c + 5, c - 10, c, now),
        )
    conn.commit()


def _add_event(conn: sqlite3.Connection, symbol: str, event_id: str = "evt_t1",
               available_at: str = "2026-08-03T16:00:00+00:00") -> None:
    """公告事件：available_at 本地日 2026-08-04 → base=08-03，T+1=08-04，T+5=08-10。"""
    now = db.utc_now()
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, published_at, published_tz,
            available_at, title, source, ingested_at)
        VALUES (?, 'announcement', ?, 'Asia/Shanghai', ?, '测试公告', 'test', ?)
        """,
        (event_id, available_at, available_at, now),
    )
    conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES (?, ?)",
        (event_id, symbol),
    )
    conn.commit()


def _raw_dir(tmp_path: Path, files: dict[str, str]) -> str:
    """按 raw 路径约定落盘：data/raw/stock_finance_data/price/{date}/{run}/{file}。"""
    d = tmp_path / "data" / "raw" / "stock_finance_data" / "price" / "2026-08-07" / "run_t1"
    d.mkdir(parents=True)
    for name, content in files.items():
        (d / name).write_text(content, encoding="utf-8")
    return str(tmp_path / "data" / "raw")


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "market.db")
    db.migrate(c)
    _add_calendar(c)
    yield c
    c.close()


def _snapshot(conn: sqlite3.Connection) -> dict:
    """派生与运行表内容快照（剔除时间戳列，用于幂等比对）。"""
    def rows(sql, exclude=()):
        return [
            {k: v for k, v in dict(r).items() if k not in exclude}
            for r in conn.execute(sql)
        ]
    return {
        "daily_bars": rows("SELECT * FROM daily_bars ORDER BY symbol, trade_date",
                           exclude=("updated_at",)),
        "weekly_bars": rows("SELECT * FROM weekly_bars ORDER BY symbol, week_end_date"),
        "indicators_daily": rows(
            "SELECT * FROM indicators_daily ORDER BY symbol, trade_date",
            exclude=("computed_at",)),
        "indicators_weekly": rows(
            "SELECT * FROM indicators_weekly ORDER BY symbol, week_end_date",
            exclude=("computed_at",)),
        "raw_objects": rows("SELECT * FROM raw_objects ORDER BY raw_object_id",
                            exclude=("ingested_at",)),
        "pipeline_runs": rows("SELECT * FROM pipeline_runs ORDER BY run_id, stage",
                              exclude=("as_of", "started_at", "finished_at")),
    }


# ---------------------------------------------------------------- 完整跑通

def test_daily_full_pipeline(conn, tmp_path):
    _add_watchlist(conn, "TEST.SH")
    _add_bars(conn, "TEST.SH", WEEK[:-1])  # 08-03~08-06 已在库
    raw = _raw_dir(tmp_path, {"TEST.SH.csv": _price_csv("TEST.SH", RUN_DATE, 104.0)})

    res = run_daily(conn, RUN_DATE, raw_dir=raw, reports_root=str(tmp_path / "reports"))

    assert res.markets == {"CN": "交易日"}
    assert len(res.symbols) == 1
    sr = res.symbols[0]
    assert sr.status == ST_OK and sr.gate == "trading_with_bars"
    # 新 bar 入库、周线（08-07 完成周）、指标全量重算
    assert conn.execute(
        "SELECT close_raw FROM daily_bars WHERE symbol='TEST.SH' AND trade_date=?",
        (RUN_DATE,)).fetchone()[0] == pytest.approx(104.0)
    assert conn.execute(
        "SELECT COUNT(*) FROM weekly_bars WHERE symbol='TEST.SH'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM indicators_daily WHERE symbol='TEST.SH'").fetchone()[0] == 5
    # pipeline_runs 阶段记录齐（§2.3 版本字段）
    stages = {r["stage"]: r for r in conn.execute(
        "SELECT * FROM pipeline_runs WHERE run_id = ?", (f"daily_{RUN_DATE}",))}
    assert {"calendar", "symbol:TEST.SH", "summary"} <= set(stages)
    assert stages["symbol:TEST.SH"]["status"] == "success"
    assert stages["summary"]["status"] == "success"
    for r in stages.values():
        assert r["adapter_version"] and r["config_hash"] and r["rule_version"]


# ---------------------------------------------------------------- 非交易日

def test_non_trading_day_skips_compute(conn, tmp_path, capsys):
    _add_watchlist(conn, "TEST.SH")
    _add_bars(conn, "TEST.SH", WEEK)

    res = run_daily(conn, "2026-08-08", reports_root=str(tmp_path / "reports"))  # 周六

    assert res.markets == {"CN": "non_trading_day（休市（weekend））"}
    assert res.symbols[0].status == ST_NON_TRADING
    # 不计算：无指标行、无 symbol 阶段之外的新数据
    assert conn.execute(
        "SELECT COUNT(*) FROM indicators_daily WHERE symbol='TEST.SH'").fetchone()[0] == 0
    # CLI 退出码 0（非交易日不报错）且输出明确
    rc = main(["--date", "2026-08-08", "--reports-root", str(tmp_path / "reports"), "--db", str(conn.execute("PRAGMA database_list").fetchone()[2])])
    out = capsys.readouterr().out
    assert rc == 0 and "non_trading_day" in out


# ---------------------------------------------------------------- 幂等重跑

def test_rerun_is_idempotent(conn, tmp_path):
    _add_watchlist(conn, "TEST.SH")
    _add_bars(conn, "TEST.SH", WEEK[:-1])
    raw = _raw_dir(tmp_path, {"TEST.SH.csv": _price_csv("TEST.SH", RUN_DATE, 104.0)})

    run_daily(conn, RUN_DATE, raw_dir=raw, reports_root=str(tmp_path / "reports"))
    first = _snapshot(conn)
    res2 = run_daily(conn, RUN_DATE, raw_dir=raw, reports_root=str(tmp_path / "reports"))
    second = _snapshot(conn)

    assert res2.symbols[0].status == ST_OK
    assert first == second  # content hash 去重 + DELETE+重插 + 阶段覆盖
    assert conn.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1
    assert any("content hash 已登记" in n for n in res2.symbols[0].notes)


# ---------------------------------------------------------------- 单股失败隔离

def test_symbol_failure_isolated(conn, tmp_path):
    _add_watchlist(conn, "BAD.SH")
    _add_watchlist(conn, "TEST.SH")
    _add_bars(conn, "BAD.SH", WEEK[:-1])
    _add_bars(conn, "TEST.SH", WEEK[:-1])
    raw = _raw_dir(tmp_path, {
        "BAD.SH.csv": _price_csv("BAD.SH", RUN_DATE, 104.0, bad=True),
        "TEST.SH.csv": _price_csv("TEST.SH", RUN_DATE, 104.0),
    })

    res = run_daily(conn, RUN_DATE, raw_dir=raw, reports_root=str(tmp_path / "reports"))
    by_symbol = {s.symbol: s for s in res.symbols}

    assert by_symbol["BAD.SH"].status == ST_FAILED
    assert "回滚" in by_symbol["BAD.SH"].reason
    assert by_symbol["TEST.SH"].status == ST_OK
    # BAD.SH 当日全部阶段回滚：新 bar 未入库、raw_objects 未登记、无指标
    assert conn.execute(
        "SELECT COUNT(*) FROM daily_bars WHERE symbol='BAD.SH' AND trade_date=?",
        (RUN_DATE,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1  # 仅 TEST.SH
    assert conn.execute(
        "SELECT COUNT(*) FROM indicators_daily WHERE symbol='BAD.SH'").fetchone()[0] == 0
    # TEST.SH 不受影响
    assert conn.execute(
        "SELECT COUNT(*) FROM indicators_daily WHERE symbol='TEST.SH'").fetchone()[0] == 5
    stages = {r["stage"]: r["status"] for r in conn.execute(
        "SELECT stage, status FROM pipeline_runs WHERE run_id = ? AND stage LIKE 'symbol:%'",
        (f"daily_{RUN_DATE}",))}
    assert stages == {"symbol:BAD.SH": "failed", "symbol:TEST.SH": "success"}
    assert {r["status"] for r in conn.execute(
        "SELECT status FROM pipeline_runs WHERE run_id=? AND stage='summary'",
        (f"daily_{RUN_DATE}",))} == {"failed"}


# ---------------------------------------------------------------- 日历缺失市场

def test_missing_calendar_market_incomplete(conn, tmp_path):
    _add_watchlist(conn, "0700.HK", market="HK")  # 无 HK 日历种子

    res = run_daily(conn, RUN_DATE, reports_root=str(tmp_path / "reports"))

    assert "incomplete" in res.markets["HK"]
    assert res.symbols[0].status == ST_INCOMPLETE
    assert "trading_calendar 缺失" in res.symbols[0].reason
    assert conn.execute(
        "SELECT COUNT(*) FROM indicators_daily").fetchone()[0] == 0
    stages = {r["stage"]: r["status"] for r in conn.execute(
        "SELECT stage, status FROM pipeline_runs WHERE run_id = ?",
        (f"daily_{RUN_DATE}",))}
    assert stages["symbol:0700.HK"] == "degraded"
    assert stages["summary"] == "degraded"


# ---------------------------------------------------------------- 事件研究阶段（§8.1 步骤 6 确定性部分）

def test_event_study_stage(conn, tmp_path):
    """预埋事件+行情+基准指数：run_daily 后 event_assessments 落 event_study_v1 行，
    pipeline_runs 有 daily 台账 event_study 阶段与 run_event_study 自记 run。"""
    _add_watchlist(conn, "TEST.SH")
    days = WEEK + ["2026-08-10"]  # 覆盖 T+5
    _add_bars(conn, "TEST.SH", days)
    _add_index_bars(conn, days)
    _add_event(conn, "TEST.SH")

    res = run_daily(conn, RUN_DATE, reports_root=str(tmp_path / "reports"))

    row = conn.execute(
        "SELECT status, model, event_study_json FROM event_assessments "
        "WHERE event_id = 'evt_t1' AND assessment_version = 'event_study_v1'",
    ).fetchone()
    assert row is not None and row["model"] == "deterministic"
    assert row["status"] == "ok"
    study = json.loads(row["event_study_json"])
    assert study["base_date"] == "2026-08-03"
    assert study["t5"]["date"] == "2026-08-10" and study["t5"]["excess"] is not None
    stages = {r["stage"]: r["status"] for r in conn.execute(
        "SELECT stage, status FROM pipeline_runs WHERE run_id = ?",
        (f"daily_{RUN_DATE}",))}
    assert stages["event_study"] == "success"
    own = conn.execute(
        "SELECT status FROM pipeline_runs "
        "WHERE run_id = ? AND stage = 'event_study'",
        (f"daily_{RUN_DATE}_event_study",)).fetchone()
    assert own is not None and own["status"] == "success"
    assert any("事件研究 event_study" in n for n in res.notes)


def test_event_study_failure_not_blocking_report(conn, tmp_path, monkeypatch):
    """event_study 内部抛错：记 degraded 不阻断报告与汇总阶段（§2.2 第 3 类）。"""
    _add_watchlist(conn, "TEST.SH")
    _add_bars(conn, "TEST.SH", WEEK)
    _add_event(conn, "TEST.SH")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(es_mod, "run_event_study", boom)

    res = run_daily(conn, RUN_DATE, reports_root=str(tmp_path / "reports"))

    assert res.symbols[0].status == ST_OK
    assert any("event_study degraded: RuntimeError: boom" in n for n in res.notes)
    stages = {r["stage"]: r["status"] for r in conn.execute(
        "SELECT stage, status FROM pipeline_runs WHERE run_id = ?",
        (f"daily_{RUN_DATE}",))}
    assert stages["event_study"] == "degraded"
    assert "report" in stages  # 报告阶段照常执行
    assert stages["summary"] == "success"
    # 异常事务回滚：不落 assessment、不留 event_study 自记 run
    assert conn.execute("SELECT COUNT(*) FROM event_assessments").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM pipeline_runs WHERE run_id = ?",
        (f"daily_{RUN_DATE}_event_study",)).fetchone()[0] == 0


# ---------------------------------------------------------------- 状态查询

def test_status_command(conn, tmp_path, capsys):
    _add_watchlist(conn, "TEST.SH")
    _add_bars(conn, "TEST.SH", WEEK)
    run_daily(conn, RUN_DATE, reports_root=str(tmp_path / "reports"))

    rc = symbol_status(conn, "TEST.SH", RUN_DATE)
    out = capsys.readouterr().out

    assert rc == 0
    assert "最近 5 个交易日" in out
    assert "2026-08-07" in out and "gate=trading_with_bars" in out
    assert "指标: pe_ttm=NULL（no_share_capital）" in out  # 无股本事件 → pe NULL 有原因码
    assert "daily_run=success" in out
