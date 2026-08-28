"""event_calendar 测试（消息面 r2 Phase 1）。

锁定：
- migration 0003：event_calendar 建表 + events/watchlist 扩列；重跑 migrate 幂等；
- 手工种子 seed_event_calendar：jsonschema 校验、incomplete_todo 跳过、cal_id upsert；
- 到期提醒窗口（calendar_due.due_items）：窗口按每行 remind_before_days，
  含两端边界日（as_of 与 as_of+remind_before_days 均计入）；排期卡复核到期并入；
- akshare 披露预约/解禁 CSV 解析幂等（cal_id 确定性 + ON CONFLICT DO NOTHING）；
- ingest 路由锁定 ("akshare", "calendar")。
"""

from __future__ import annotations

import csv
import json

import jsonschema
import pytest

from scripts.adapters import event_calendar as ec_adapter
from scripts.adapters.common import ingest_file
from scripts.pipeline import db as pdb
from scripts.signals import calendar_due

AS_OF = "2026-08-07"  # 周五


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(tmp_path / "market.db")
    pdb.migrate(c)
    yield c
    c.close()


def _add_cal(conn, cal_id, kind, symbol, scheduled_date, *, remind=3, note=None,
             source="manual"):
    conn.execute(
        """
        INSERT INTO event_calendar (cal_id, kind, symbol, scheduled_date, source,
                                    remind_before_days, note, raw_object_id, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'test')
        """,
        (cal_id, kind, symbol, scheduled_date, source, remind, note),
    )


# ---------------------------------------------------------------- migration 0003

def test_migration_0003_schema_and_reapply(tmp_path):
    c = pdb.connect(tmp_path / "m.db")
    applied = pdb.migrate(c)
    assert [name for name in applied if name.startswith("0003")] == \
        ["0003_message_calendar.sql"]
    cols = lambda t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    assert cols("event_calendar") == [
        "cal_id", "kind", "symbol", "scheduled_date", "source",
        "remind_before_days", "note", "raw_object_id", "ingested_at"]
    assert "scope" in cols("events") and "source_tier" in cols("events")
    assert "industry_code" in cols("watchlist") and "themes_json" in cols("watchlist")
    # 幂等：重跑无新增
    assert pdb.migrate(c) == []
    c.close()


# ---------------------------------------------------------------- 手工种子

def test_seed_event_calendar_upsert_validation_and_skip(conn, tmp_path):
    """jsonschema 校验 + upsert（编辑 yaml 重跑生效）+ incomplete_todo 跳过（§2.5）。"""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        """
status: ok
events:
  - cal_id: fomc_test_1
    kind: fomc
    symbol: null
    scheduled_date: "2026-09-16"
    note: "FOMC 议息"
  - cal_id: cpi_test_1
    kind: macro_release
    scheduled_date: "2026-09-09"
    remind_before_days: 5
""", encoding="utf-8")
    assert pdb.seed_event_calendar(conn, path) == 2
    row = conn.execute(
        "SELECT * FROM event_calendar WHERE cal_id='fomc_test_1'").fetchone()
    assert row["source"] == "manual" and row["remind_before_days"] == 3
    assert row["symbol"] is None
    row2 = conn.execute(
        "SELECT * FROM event_calendar WHERE cal_id='cpi_test_1'").fetchone()
    assert row2["remind_before_days"] == 5

    # upsert：改 note 重跑生效，不新增行
    path.write_text(path.read_text(encoding="utf-8").replace("FOMC 议息", "FOMC 议息 v2"),
                    encoding="utf-8")
    assert pdb.seed_event_calendar(conn, path) == 2
    assert conn.execute("SELECT COUNT(*) FROM event_calendar").fetchone()[0] == 2
    assert conn.execute("SELECT note FROM event_calendar WHERE cal_id='fomc_test_1'"
                        ).fetchone()["note"] == "FOMC 议息 v2"

    # schema 违规拒绝（未知 kind）
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "status: ok\nevents:\n  - cal_id: x\n    kind: unknown_kind\n"
        "    scheduled_date: '2026-09-16'\n", encoding="utf-8")
    with pytest.raises(jsonschema.ValidationError):
        pdb.seed_event_calendar(conn, bad)

    # incomplete_todo 跳过，不清表
    todo = tmp_path / "todo.yaml"
    todo.write_text("status: incomplete_todo\nevents: []\n", encoding="utf-8")
    assert pdb.seed_event_calendar(conn, todo) == 0
    assert conn.execute("SELECT COUNT(*) FROM event_calendar").fetchone()[0] == 2


def test_project_seed_file_loads():
    """仓库自带 config/event_calendar.yaml 必须通过自家 schema（防手编辑破坏）。"""
    import yaml
    doc = yaml.safe_load((pdb.CONFIG_DIR / "event_calendar.yaml").read_text(encoding="utf-8"))
    jsonschema.validate(doc, pdb._EVENT_CALENDAR_SCHEMA)


# ---------------------------------------------------------------- 到期窗口

def test_due_items_window_boundaries(conn):
    """窗口 = [as_of, as_of + remind_before_days]，含两端边界日；卡片复核到期并入。"""
    _add_cal(conn, "past", "unlock", "TRIG.SH", "2026-08-06")             # as_of 前：排除
    _add_cal(conn, "on_asof", "report_disclosure", "TRIG.SH", AS_OF)      # 边界首日：含
    _add_cal(conn, "within", "macro_release", None, "2026-08-09")         # 窗口内宏观：含
    _add_cal(conn, "edge3", "unlock", "TRIG.SH", "2026-08-10")            # 默认 remind=3 边界：含
    _add_cal(conn, "beyond3", "unlock", "TRIG.SH", "2026-08-11")          # 窗口外：排除
    _add_cal(conn, "edge7", "report_disclosure", "TRIG.SH", "2026-08-14",
             remind=7)                                                    # remind=7 边界：含
    _add_cal(conn, "beyond7", "report_disclosure", "TRIG.SH", "2026-08-15",
             remind=7)                                                    # 排除
    # active 卡复核到期（next_review <= as_of）并入；未来/非 active 不并
    conn.execute(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status,
            schema_version, created_at, effective_from, currency, price_basis,
            price_tiers_json, invalidation_json, right_side_trigger_json,
            next_review_at, run_id)
        VALUES ('cv_due', 'TRIG.SH', 'active', 'card_v1', 'test', '2026-08-03',
                'CNY', 'raw', '{}', '{}', '{}', '2026-08-07', 'test')
        """)
    conn.execute(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status,
            schema_version, created_at, effective_from, currency, price_basis,
            price_tiers_json, invalidation_json, right_side_trigger_json,
            next_review_at, run_id)
        VALUES ('cv_future', 'CALM.SH', 'active', 'card_v1', 'test', '2026-08-03',
                'CNY', 'raw', '{}', '{}', '{}', '2026-09-01', 'test')
        """)
    conn.commit()

    items = calendar_due.due_items(conn, AS_OF)
    got = {(it["kind"], it["symbol"], it["date"]) for it in items}
    assert ("report_disclosure", "TRIG.SH", AS_OF) in got           # on_asof（边界首日）
    assert ("macro_release", None, "2026-08-09") in got             # within（宏观）
    assert ("unlock", "TRIG.SH", "2026-08-10") in got               # edge3
    assert ("report_disclosure", "TRIG.SH", "2026-08-14") in got    # edge7（remind=7）
    assert ("unlock", "TRIG.SH", "2026-08-11") not in got           # beyond3
    assert ("report_disclosure", "TRIG.SH", "2026-08-15") not in got  # beyond7
    assert ("unlock", "TRIG.SH", "2026-08-06") not in got           # past
    assert ("card_review", "TRIG.SH", AS_OF) in got              # cv_due
    assert not any(it["note"] and "cv_future" in it["note"] for it in items)
    card = [it for it in items if it["kind"] == calendar_due.KIND_CARD_REVIEW][0]
    assert card["note"] and "cv_due" in card["note"]

    # 单股过滤：本股 + 宏观 + 本股卡片；他人卡片不出现
    here = calendar_due.relevant_to_symbol(items, "TRIG.SH")
    assert len(here) == 5
    assert all(it["symbol"] in ("TRIG.SH", None) for it in here)


# ---------------------------------------------------------------- akshare CSV 解析

def _disclosure_csv(path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "name", "period", "scheduled_date",
                    "first_scheduled", "actual_disclosed"])
        w.writerow(["603605.SH", "珀莱雅", "2026半年报", "2026-08-20",
                    "2026-08-10", ""])
        w.writerow(["601088.SH", "中国神华", "2026半年报", "2026-08-28",
                    "2026-08-28", "2026-08-28"])


def _unlock_csv(path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "unlock_date", "shares_free", "ratio_total", "share_type"])
        w.writerow(["601088.SH", "2026-10-08", "457665900", "0.021101",
                    "定向增发机构配售股份"])
        w.writerow(["601088.SH", "2029-03-16", "1363248000", "0.062853",
                    "定向增发机构配售股份"])


def test_parse_calendar_csv_and_idempotent(conn, tmp_path):
    """披露预约/解禁解析：kind/note 正确、坏行跳过、重跑幂等、未知 stem 跳过。"""
    pd = tmp_path / "raw" / "akshare" / "calendar" / "2026-08-28" / "run_ak" / "report_disclosure.csv"
    pu = tmp_path / "raw" / "akshare" / "calendar" / "2026-08-28" / "run_ak" / "unlock.csv"
    pd.parent.mkdir(parents=True)
    _disclosure_csv(pd)
    _unlock_csv(pu)

    rd = ingest_file(conn, pd, source="akshare", data_type="calendar",
                     symbol=None, parse=ec_adapter.parse_calendar_csv)
    assert rd.status == "ok", rd.summary()
    assert rd.inserted == 2
    row = conn.execute(
        "SELECT * FROM event_calendar WHERE symbol='603605.SH'").fetchone()
    assert row["kind"] == "report_disclosure" and row["source"] == "akshare"
    assert row["scheduled_date"] == "2026-08-20"
    assert "首次预约 2026-08-10（已变更）" in row["note"]
    assert row["cal_id"].startswith("cal_")
    row_hk = conn.execute(
        "SELECT * FROM event_calendar WHERE symbol='601088.SH' "
        "AND kind='report_disclosure'").fetchone()
    assert "已实际披露" in row_hk["note"]

    ru = ingest_file(conn, pu, source="akshare", data_type="calendar",
                     symbol=None, parse=ec_adapter.parse_calendar_csv)
    assert ru.status == "ok", ru.summary()
    assert ru.inserted == 2
    unl = conn.execute(
        "SELECT * FROM event_calendar WHERE kind='unlock' AND scheduled_date='2026-10-08'"
    ).fetchone()
    assert unl["symbol"] == "601088.SH"
    assert "解禁 4.58 亿股" in unl["note"] and "占总市值 2.11%" in unl["note"]

    # 幂等：同文件重跑被 content_hash 门槛整文件跳过（skipped=1，零新增）
    _add_cal(conn, "manual_row", "macro_release", None, "2026-09-16", note="FOMC")
    conn.commit()
    rd2 = ingest_file(conn, pd, source="akshare", data_type="calendar",
                      symbol=None, parse=ec_adapter.parse_calendar_csv)
    ru2 = ingest_file(conn, pu, source="akshare", data_type="calendar",
                      symbol=None, parse=ec_adapter.parse_calendar_csv)
    assert rd2.inserted == 0 and ru2.inserted == 0
    assert rd2.skipped == 1 and ru2.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM event_calendar").fetchone()[0] == 5

    # 未知 stem：跳过并记 note
    bad = tmp_path / "raw" / "akshare" / "calendar" / "2026-08-28" / "run_ak" / "other.csv"
    bad.write_text("x\n", encoding="utf-8")
    rb = ingest_file(conn, bad, source="akshare", data_type="calendar",
                     symbol=None, parse=ec_adapter.parse_calendar_csv)
    assert rb.status == "ok"
    assert any("未知文件 stem" in n for n in rb.notes)


def test_calendar_route_registered():
    """ingest 路由表锁定：(akshare, calendar) → adapters/event_calendar。"""
    from scripts.pipeline.ingest import _ROUTES
    assert _ROUTES[("akshare", "calendar")] is ec_adapter.parse_calendar_csv
