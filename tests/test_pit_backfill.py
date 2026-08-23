"""财报披露日匹配与回填测试（scripts/pipeline/pit_backfill.py）。"""

import csv
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts.adapters.tianyancha import _next_open_available_at
from scripts.adapters.common import load_calendar
from scripts.pipeline import db as pipeline_db
from scripts.pipeline.pit_backfill import (
    AnnouncementRow,
    load_announcements,
    match_disclosure,
    run_pit_backfill,
    title_keywords,
)


def _ann(title, date, *, uuid=None, stock_code="603605", source_file="t_p1.csv"):
    return AnnouncementRow(title=title, date=date, uuid=uuid,
                           stock_code=stock_code, source_file=source_file)


# ---------------------------------------------------------------- 标题关键词

def test_title_keywords_mapping():
    assert title_keywords("annual", "2023-12-31", 2023) == ["2023年年度报告"]
    assert title_keywords("quarterly", "2024-03-31", 2024) == [
        "2024年第一季度报告", "2024年一季度报告"]
    assert title_keywords("interim", "2024-06-30", 2024) == ["2024年半年度报告"]
    assert title_keywords("quarterly", "2024-09-30", 2024) == [
        "2024年第三季度报告", "2024年三季度报告"]
    assert title_keywords("quarterly", "2024-12-31", 2024) == []  # 非法组合不匹配


# ---------------------------------------------------------------- 匹配规则

def test_full_text_earliest_wins_and_revision_excluded():
    """同标题多条取最早；修订版/更正版/（更正后）不影响首披日。"""
    anns = [
        _ann("珀莱雅:珀莱雅化妆品股份有限公司2023年年度报告（修订版）", "2024-05-10"),
        _ann("珀莱雅:珀莱雅化妆品股份有限公司2023年年度报告", "2024-04-19"),
        _ann("珀莱雅:珀莱雅化妆品股份有限公司2023年年度报告摘要", "2024-04-19"),
        _ann("珀莱雅:关于2023年年度报告的更正公告", "2024-04-25"),
    ]
    m = match_disclosure("annual", "2023-12-31", 2023, anns)
    assert m is not None
    assert m.disclosure_date == "2024-04-19"
    assert m.tier == "full"
    assert "摘要" not in m.title and "修订" not in m.title


def test_english_edition_skipped_no_guess():
    """只有英文版/修订版时视为未匹配（不猜）。"""
    anns = [
        _ann("恒力石化:恒力石化2025年年度报告（英文版）", "2026-05-12"),
        _ann("豫光金铅:河南豫光金铅股份有限公司2025年年度报告（更正后）", "2026-04-29"),
    ]
    assert match_disclosure("annual", "2025-12-31", 2025, anns) is None


def test_abstract_fallback_when_no_full_text():
    """缺全文时摘要可代表同日首披。"""
    anns = [_ann("某股:某公司2024年半年度报告摘要", "2024-08-20")]
    m = match_disclosure("interim", "2024-06-30", 2024, anns)
    assert m is not None
    assert m.disclosure_date == "2024-08-20" and m.tier == "abstract"


def test_full_text_preferred_over_abstract():
    anns = [
        _ann("某股:某公司2024年半年度报告摘要", "2024-08-20"),
        _ann("某股:某公司2024年半年度报告", "2024-08-20"),
    ]
    m = match_disclosure("interim", "2024-06-30", 2024, anns)
    assert m.tier == "full" and "摘要" not in m.title


def test_quarterly_name_variants():
    """深市常用「一季度报告」，沪市用「第一季度报告」，两种都认。"""
    m1 = match_disclosure("quarterly", "2026-03-31", 2026,
                          [_ann("圣农发展:2026年一季度报告", "2026-04-23")])
    m2 = match_disclosure("quarterly", "2026-03-31", 2026,
                          [_ann("珀莱雅:2026年第一季度报告", "2026-04-22")])
    assert m1 and m1.disclosure_date == "2026-04-23"
    assert m2 and m2.disclosure_date == "2026-04-22"


def test_hk_rows_filtered_by_stock_code():
    """A+H 混排：H 股行（stock_code=HK.xxxx）即使标题命中也不算。"""
    anns = [_ann("海外监管公告 - 中国平安2026年第一季度报告", "2026-04-28",
                 stock_code="HK.02318")]
    assert match_disclosure("quarterly", "2026-03-31", 2026, anns,
                            code6="601318") is None


def test_non_disclosure_titles_excluded():
    """问询函/说明会/审计报告等含关键词但不是首披文本。"""
    anns = [
        _ann("某股:关于公司2023年年度报告的信息披露监管问询函回复公告", "2024-05-20"),
        _ann("某股:关于召开2023年年度报告业绩说明会的公告", "2024-04-20"),
    ]
    assert match_disclosure("annual", "2023-12-31", 2023, anns) is None


def test_no_match_returns_none():
    assert match_disclosure("annual", "2023-12-31", 2023, []) is None


def test_load_announcements_dedup_and_filter(tmp_path):
    """CSV 加载：按 uuid 去重、过滤 H 股行。"""
    path = tmp_path / "603605.SH_p1.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stock_name", "ossUrl", "companyName", "name", "id", "time",
                    "announcementType", "title", "uuid", "stock_code"])
        w.writerow(["珀莱雅", "u1", "珀莱雅化妆品股份有限公司", "珀莱雅", "1",
                    "2024-04-19", "年度报告全文", "珀莱雅:2023年年度报告", "aaa", "603605"])
        w.writerow(["珀莱雅", "u1", "珀莱雅化妆品股份有限公司", "珀莱雅", "1",
                    "2024-04-19", "年度报告全文", "珀莱雅:2023年年度报告", "aaa", "603605"])
        w.writerow(["珀莱雅", "u2", "珀莱雅化妆品股份有限公司", "珀莱雅", "2",
                    "2024-04-19", "年報", "二零二三年年报", "bbb", "HK.06036"])
    rows = load_announcements([tmp_path], "603605.SH")
    assert len(rows) == 1 and rows[0].uuid == "aaa"


# ---------------------------------------------------------------- 回填（DB）

@pytest.fixture()
def conn(tmp_path):
    c = pipeline_db.connect(tmp_path / "market.db")
    pipeline_db.migrate(c)
    pipeline_db.seed(c)
    now = pipeline_db.utc_now()
    c.execute(
        """
        INSERT INTO financial_reports (symbol, period_end, period_type, fiscal_year,
            published_at, published_tz, available_at, revision, currency, unit,
            is_cumulative, raw_object_id, ingested_at)
        VALUES ('603605.SH', '2023-12-31', 'annual', 2023, NULL, NULL,
                '2026-08-09T13:00:00+00:00', 1, 'CNY', 'yuan', 1, NULL, ?),
               ('603605.SH', '2024-03-31', 'quarterly', 2024, NULL, NULL,
                '2026-08-09T13:00:01+00:00', 1, 'CNY', 'yuan', 1, NULL, ?)
        """,
        (now, now),
    )
    c.commit()
    yield c
    c.close()


def _write_csv(dir_path, symbol, rows):
    path = dir_path / f"{symbol}_p1.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stock_name", "ossUrl", "companyName", "name", "id", "time",
                    "announcementType", "title", "uuid", "stock_code"])
        for i, (title, date) in enumerate(rows):
            w.writerow(["珀莱雅", "u", "珀莱雅化妆品股份有限公司", "珀莱雅", str(i),
                        date, "", title, f"uuid{i}", "603605"])
    return path


def test_backfill_updates_matched_and_keeps_unmatched(conn, tmp_path):
    _write_csv(tmp_path, "603605.SH", [
        ("珀莱雅:珀莱雅化妆品股份有限公司2023年年度报告", "2024-04-19"),
    ])
    backup = tmp_path / "backup.csv"
    with conn:
        res = run_pit_backfill(
            conn, symbols=["603605.SH"], raw_dirs=[tmp_path],
            run_id="run_pit_t1", backup_path=backup)
    assert res.matched == 1 and len(res.unmatched) == 1
    assert res.unmatched[0]["period_end"] == "2024-03-31"  # 不匹配不猜

    row = conn.execute(
        "SELECT published_at, published_tz, available_at FROM financial_reports "
        "WHERE symbol='603605.SH' AND period_end='2023-12-31'").fetchone()
    tz_sh = datetime.fromisoformat("2024-04-19T00:00:00+08:00")
    assert row["published_at"] == tz_sh.astimezone(timezone.utc).isoformat()
    assert row["published_tz"] == "Asia/Shanghai"
    assert row["available_at"] == _next_open_available_at(
        load_calendar(conn, "CN"), "2024-04-19")

    # 未匹配行保持降级原值
    row2 = conn.execute(
        "SELECT published_at, available_at FROM financial_reports "
        "WHERE symbol='603605.SH' AND period_end='2024-03-31'").fetchone()
    assert row2["published_at"] is None
    assert row2["available_at"] == "2026-08-09T13:00:01+00:00"

    # data_revisions 留痕（含天眼查 uuid）
    rev = conn.execute(
        "SELECT * FROM data_revisions WHERE table_name='financial_reports'").fetchone()
    assert rev is not None and rev["run_id"] == "run_pit_t1"
    assert "uuid0" in rev["new_value"]

    # raw_objects 登记 + 备份 CSV
    assert conn.execute(
        "SELECT COUNT(*) c FROM raw_objects WHERE source='tianyancha' "
        "AND data_type='announcement'").fetchone()["c"] == 1
    with open(backup, encoding="utf-8") as f:
        assert len(list(csv.reader(f))) == 1 + 2  # 表头 + 2 行

    # 幂等：重跑不再改（published_at 已有值仍按同公告重算为同值）
    with conn:
        res2 = run_pit_backfill(
            conn, symbols=["603605.SH"], raw_dirs=[tmp_path], run_id="run_pit_t2")
    assert res2.matched == 1 and len(res2.unmatched) == 1


def test_backfill_dry_run_writes_nothing(conn, tmp_path):
    _write_csv(tmp_path, "603605.SH", [
        ("珀莱雅:珀莱雅化妆品股份有限公司2023年年度报告", "2024-04-19"),
    ])
    res = run_pit_backfill(
        conn, symbols=["603605.SH"], raw_dirs=[tmp_path],
        run_id="run_pit_dry", dry_run=True)
    assert res.matched == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM financial_reports WHERE published_at IS NOT NULL"
    ).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM data_revisions").fetchone()["c"] == 0
