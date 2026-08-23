"""D2.6 报告生成器测试（设计 §6.1-6.4、§9.5、§8.1）。

锁定：
- 单股报告七段结构齐全（§6.2）；无触发明写"今日无决策点"；
- 衰竭信号段带锚点明细（§5.2 ⚠️）；观察点带临近度（距边界/干涸阈值差值）；
- 全池日报五级确定性排序：数据异常股排在触发股之前（§6.3），排序原因显示；
- 语言纪律：报告不含"看涨/看跌/建议买入"等词（§6.4）；
- report_runs：每份报告一行；同日重跑生成 revision 新行、同名文件覆盖（§9.5）；
- pipeline 集成：信号+报告阶段挂入后全链路幂等（信号事实重跑一致，
  report_runs revision 增长是设计行为）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.pipeline import db
from scripts.pipeline import report as report_mod
from scripts.pipeline.daily import run_daily

RUN_DATE = "2026-08-07"  # 周五
WEEK = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]


def _add_calendar(conn: sqlite3.Connection, market: str = "CN") -> None:
    now = db.utc_now()
    d, end = date(2026, 8, 1), date(2026, 8, 31)
    while d <= end:
        is_open = 1 if d.weekday() < 5 else 0
        conn.execute(
            """
            INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                session_open, session_close, status, status_detail, timezone,
                source, updated_at)
            VALUES (?, ?, ?, 1, NULL, NULL, ?, NULL, 'Asia/Shanghai', 'test', ?)
            """,
            (market, d.isoformat(), is_open, "trading" if is_open else "weekend", now),
        )
        d += timedelta(days=1)


def _add_watchlist(conn, symbol, name="测试"):
    now = db.utc_now()
    conn.execute(
        """
        INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,
                               currency, timezone, active, created_at, updated_at)
        VALUES (?, 'CN', ?, '[]', '000300.SH', 'CNY', 'Asia/Shanghai', 1, ?, ?)
        """,
        (symbol, name, now, now),
    )


def _add_bars(conn, symbol, days, base=100.0):
    now = db.utc_now()
    for i, d in enumerate(days):
        p = base + i
        conn.execute(
            """
            INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
                low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
                trading_status, source, updated_at)
            VALUES (?, ?, 'CN', ?, ?, ?, ?, 10000, 1.0, 1.0, 'normal', 'test', ?)
            """,
            (symbol, d, p - 0.3, p + 0.5, p - 0.6, p, now),
        )


def _add_indicator(conn, symbol, day, **kw):
    cols = {"ma5": 101.0, "ma20": 100.0, "ma60": 99.0, "dif": 0.1, "dea": 0.2,
            "macd_hist": -0.2, "rsi12": 45.0, "boll_mid": 100.0, "vol_ma5": 9000.0,
            "vol_mean20": 9500.0, "vol_std20": 500.0, "pct_chg": 0.5,
            "pe_ttm": 15.5, "pe_status": "ok"}
    cols.update(kw)
    keys = ", ".join(cols)
    conn.execute(
        f"INSERT INTO indicators_daily (symbol, trade_date, {keys}, computed_at) "
        f"VALUES (?, ?, {', '.join('?' * len(cols))}, ?)",
        (symbol, day, *cols.values(), db.utc_now()),
    )


def _add_card(conn, symbol, card_id, tiers, eff_from="2026-08-03"):
    conn.execute(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status,
            schema_version, created_at, effective_from, currency, price_basis,
            price_tiers_json, invalidation_json, right_side_trigger_json,
            next_review_at, run_id)
        VALUES (?, ?, 'active', 'card_v1', ?, ?, 'CNY', 'raw', ?, ?, ?, '2026-09-01',
                'test')
        """,
        (card_id, symbol, db.utc_now(), eff_from,
         json.dumps({"tiers": tiers}, sort_keys=True),
         json.dumps({"line": "90.00"}),
         json.dumps({"trigger_level": "120.00", "stop_level": "110.00"})),
    )


def _add_fact(conn, symbol, day, signal, state, triggered=0, details=None,
              anchor_id=None):
    conn.execute(
        """
        INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
            triggered, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, day, signal, state, anchor_id, triggered,
         json.dumps(details or {}, ensure_ascii=False, sort_keys=True), db.utc_now()),
    )


@pytest.fixture()
def conn(tmp_path):
    """三股票 fixture：ANOM.SH（数据异常）/ TRIG.SH（第一档触发）/ CALM.SH（无触发）。"""
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    _add_calendar(c)
    for s, n in (("ANOM.SH", "异常股"), ("TRIG.SH", "触发股"), ("CALM.SH", "平稳股")):
        _add_watchlist(c, s, n)

    # ANOM.SH：08-07 无 bar、基准也无 bar → source_missing（数据异常 P1）
    _add_bars(c, "ANOM.SH", WEEK[:-1])

    # TRIG.SH：现价 104（进入 T1 [100,110]）→ 档位触发（P3）
    _add_bars(c, "TRIG.SH", WEEK)
    _add_indicator(c, "TRIG.SH", RUN_DATE)
    _add_card(c, "TRIG.SH", "cv_trig",
              [{"tier": 1, "zone_low": "100.00", "zone_high": "110.00"},
               {"tier": 2, "zone_low": "85.00", "zone_high": "92.00"}])
    _add_fact(c, "TRIG.SH", RUN_DATE, "tier_triggered", "triggered", 1, {
        "close_raw": "104.00", "min_active_signals": 2,
        "tiers": [{"tier": 1, "zone_low": "100.00", "zone_high": "110.00",
                   "in_zone": True, "requires_signals": False,
                   "distance_to_nearest_boundary_pct": 0.0}]})
    _add_fact(c, "TRIG.SH", RUN_DATE, "tier_proximity", "inactive", 0, {
        "close_raw": "104.00", "threshold_pct": 0.03,
        "tiers": [{"tier": 2, "zone_low": "85.00", "zone_high": "92.00",
                   "in_zone": False, "nearest_boundary": "92.00",
                   "distance_to_nearest_boundary_pct": 0.1304,
                   "within_proximity": False}]})
    _add_fact(c, "TRIG.SH", RUN_DATE, "falsification_breach", "inactive", 0, {
        "close_raw": "104.00", "invalidation_line": "90.00", "breach_pct": 0.01,
        "breach_threshold": "89.10", "breached_today": False,
        "consecutive_breach_days": 0, "confirm_days": 2, "reason": "no_breach"})
    _add_fact(c, "TRIG.SH", RUN_DATE, "box_position", "mid_box", 0,
              {"close_raw": "104.00", "boundaries": {"box_low": "90.00"}})
    _add_fact(c, "TRIG.SH", RUN_DATE, "ma_comparison", "above", 0, {
        "close_raw": 104.00, "price_adj_factor": 1.0,
        "mas": {"ma20": {"adjusted": 100.0, "raw_equiv": 100.0, "position": "above"}}})
    _add_fact(c, "TRIG.SH", RUN_DATE, "right_side", "idle", 0, {"reason": "no_episode"})
    # 周线锚点 + 五项衰竭信号（含干涸阈值差值，§5.2 ⚠️ 锚点明细）
    c.execute(
        """
        INSERT INTO weekly_bars (symbol, week_end_date, week_start_date, open_adj,
            high_adj, low_adj, close_adj, volume_adj, trading_days)
        VALUES ('TRIG.SH', '2026-08-07', '2026-08-03', 100, 106, 99, 104, 50000, 5)
        """)
    cur = c.execute(
        """
        INSERT INTO weekly_anchors (symbol, as_of, anchor_type, trade_date,
            adjusted_price, raw_price, is_fallback, created_at)
        VALUES ('TRIG.SH', '2026-08-07', 'panic_low', '2026-06-01', 95.5, 94.0, 0, ?)
        """, (db.utc_now(),))
    anchor_id = cur.lastrowid
    c.execute(
        """
        INSERT INTO weekly_anchors (symbol, as_of, anchor_type, trade_date,
            adjusted_price, raw_price, is_fallback, created_at)
        VALUES ('TRIG.SH', '2026-08-07', 'decline_start', '2026-02-06', 130.2,
                128.0, 0, ?)
        """, (db.utc_now(),))
    anchor = {"anchor_date": "2026-06-01", "anchor_adjusted_price": 95.5,
              "anchor_raw_price": 94.0, "decline_date": "2026-02-06",
              "decline_adjusted_close": 130.2, "is_fallback": False}
    _add_fact(c, "TRIG.SH", RUN_DATE, "panic", "inactive", 0,
              {"anchor": anchor, "reason": "condition_not_met"}, anchor_id)
    _add_fact(c, "TRIG.SH", RUN_DATE, "dry_up", "inactive", 0, {
        "anchor": anchor, "reason": "condition_not_met",
        "current_volume": 50000.0, "threshold_volume": 20000.0,
        "base_mean": 40000.0, "vol_ratio_threshold": 0.5}, anchor_id)
    _add_fact(c, "TRIG.SH", RUN_DATE, "no_new_low_3w", "inactive", 0,
              {"anchor": anchor, "reason": "broken_before_confirm"}, anchor_id)
    _add_fact(c, "TRIG.SH", RUN_DATE, "divergence", "inactive", 0,
              {"anchor": anchor, "reason": "condition_not_met"}, anchor_id)
    _add_fact(c, "TRIG.SH", RUN_DATE, "duration", "active", 0, {
        "anchor": anchor, "reason": "condition_met", "elapsed_weeks": 25,
        "required_weeks": 8}, anchor_id)

    # CALM.SH：价区远离现价（[50,60]，现价 104）→ 无触发（P5）
    _add_bars(c, "CALM.SH", WEEK)
    _add_indicator(c, "CALM.SH", RUN_DATE)
    _add_card(c, "CALM.SH", "cv_calm",
              [{"tier": 1, "zone_low": "50.00", "zone_high": "60.00"}])
    _add_fact(c, "CALM.SH", RUN_DATE, "tier_triggered", "inactive", 0, {
        "close_raw": "104.00", "min_active_signals": 2, "tiers": []})
    _add_fact(c, "CALM.SH", RUN_DATE, "tier_proximity", "inactive", 0, {
        "close_raw": "104.00", "threshold_pct": 0.03,
        "tiers": [{"tier": 1, "zone_low": "50.00", "zone_high": "60.00",
                   "in_zone": False, "nearest_boundary": "60.00",
                   "distance_to_nearest_boundary_pct": 0.7333,
                   "within_proximity": False}]})
    _add_fact(c, "CALM.SH", RUN_DATE, "falsification_breach", "inactive", 0, {
        "close_raw": "104.00", "invalidation_line": "90.00", "breach_pct": 0.01,
        "breach_threshold": "89.10", "breached_today": False,
        "consecutive_breach_days": 0, "confirm_days": 2, "reason": "no_breach"})
    c.commit()
    yield c
    c.close()


def _run(conn, tmp_path):
    return report_mod.run_reports(conn, RUN_DATE,
                                  reports_root=str(tmp_path / "reports"))


# ---------------------------------------------------------------- 七段结构

def test_single_report_seven_sections(conn, tmp_path):
    res = _run(conn, tmp_path)
    by_symbol = {s.symbol: s for s in res.symbols}
    text = Path(by_symbol["TRIG.SH"].file_path).read_text(encoding="utf-8") \
        if Path(by_symbol["TRIG.SH"].file_path).is_absolute() else \
        (tmp_path / "reports" / "TRIG.SH" / f"{RUN_DATE}.md").read_text(encoding="utf-8")
    for section in ("## 1. 运行状态", "## 2. 当前定位", "## 3. 决策点",
                    "## 4. 观察点", "## 5. 衰竭信号", "## 6. 指标快照",
                    "## 7. 来源与异常"):
        assert section in text
    # 关键数字带来源与截止（§6.4）
    assert "来源 daily_bars" in text and "截止 2026-08-07" in text
    assert "config_hash" in text and "signals_v1" in text
    # 消息评价口径如实表述：LLM 评价（D3）未接入；确定性事件研究已接入
    assert "LLM 消息评价（D3）未接入" in text
    assert "确定性事件研究 event_study_v1 已接入" in text
    assert "event_assessments 未接入" not in text


def test_decision_point_and_no_decision(conn, tmp_path):
    res = _run(conn, tmp_path)
    by_symbol = {s.symbol: s for s in res.symbols}
    trig_md = (tmp_path / "reports" / "TRIG.SH" / f"{RUN_DATE}.md").read_text(encoding="utf-8")
    calm_md = (tmp_path / "reports" / "CALM.SH" / f"{RUN_DATE}.md").read_text(encoding="utf-8")
    assert "[档位触发 T1]" in trig_md
    assert "今日无决策点" in calm_md  # 无触发明写（§6.2）
    assert by_symbol["TRIG.SH"].priority == 3
    assert by_symbol["CALM.SH"].priority == 5


def test_exhaustion_section_has_anchor_details(conn, tmp_path):
    _run(conn, tmp_path)
    md = (tmp_path / "reports" / "TRIG.SH" / f"{RUN_DATE}.md").read_text(encoding="utf-8")
    # §5.2 ⚠️：锚点日期、起点日期、计算值必须带出
    assert "恐慌低点锚点: 交易日 2026-06-01" in md
    assert "下跌起点锚点: 2026-02-06" in md
    assert "95.5000" in md and "130.2000" in md
    assert "anchor_id=" in md
    assert "活跃信号 1 项" in md and "duration" in md
    # 观察点临近度：距第二档边界百分比 + 干涸阈值差值（details_json 取数）
    assert "距上沿 92.00 还差 13.0%" in md
    assert "干涸阈值" in md and "距阈值还差" in md


def test_message_section_event_study_counts(conn, tmp_path):
    """§7 来源与异常：event_study_v1 行存在时带出状态分布与 pending 终点计数。"""
    now = db.utc_now()
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, available_at, title, source,
                            ingested_at)
        VALUES ('evt_r1', 'announcement', '2026-08-03T16:00:00+00:00', '测试公告',
                'test', ?)
        """, (now,))
    conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES ('evt_r1', 'TRIG.SH')")
    conn.execute(
        """
        INSERT INTO event_assessments (event_id, symbol, assessment_version, model,
            assessed_at, event_type, status, event_study_json, run_id)
        VALUES ('evt_r1', 'TRIG.SH', 'event_study_v1', 'deterministic', ?, 'announcement',
                'ok', '{"t1": {"mark": null}, "t5": {"mark": "pending"}}', 'test')
        """, (now,))
    conn.commit()

    _run(conn, tmp_path)
    md = (tmp_path / "reports" / "TRIG.SH" / f"{RUN_DATE}.md").read_text(
        encoding="utf-8")

    assert ("确定性事件研究 event_study_v1 已接入: 库内 1 条"
            "（ok 1，含 pending 终点 1 条，来源 event_assessments）") in md
    assert "LLM 消息评价（D3）未接入" in md


# ---------------------------------------------------------------- 全池排序（§6.3）
def test_daily_report_priority_ordering(conn, tmp_path):
    res = _run(conn, tmp_path)
    daily_md = Path(res.daily_path)
    if not daily_md.is_absolute():
        daily_md = tmp_path / "reports" / "daily" / f"{RUN_DATE}.md"
    text = daily_md.read_text(encoding="utf-8")
    # 数据异常股排在触发股之前（P1 < P3 < P5）
    i_anom = text.index("ANOM.SH")
    i_trig = text.index("TRIG.SH")
    i_calm = text.index("CALM.SH")
    assert i_anom < i_trig < i_calm
    for title in report_mod.PRIORITY_TITLES.values():
        assert f"## {title}" in text
    assert "排序原因" in text  # 同级排序规则显示
    by_symbol = {s.symbol: s for s in res.symbols}
    assert by_symbol["ANOM.SH"].priority == 1
    assert by_symbol["ANOM.SH"].status == "degraded"  # source_missing


# ---------------------------------------------------------------- 语言纪律（§6.4）

def test_language_discipline(conn, tmp_path):
    _run(conn, tmp_path)
    for path in (tmp_path / "reports").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for word in report_mod.BANNED_WORDS:
            assert word not in text, f"{path.name} 含违禁词 {word!r}（§6.4）"
    assert {"看涨", "看跌", "建议买入"} <= set(report_mod.BANNED_WORDS)


# ---------------------------------------------------------------- report_runs 与 revision（§9.5）

def test_report_runs_and_revision(conn, tmp_path):
    res1 = _run(conn, tmp_path)
    rows = conn.execute(
        "SELECT report_type, symbol, revision, status, card_version_id, file_path "
        "FROM report_runs ORDER BY report_type, symbol").fetchall()
    assert len(rows) == 4  # single×3 + daily×1
    assert {r["report_type"] for r in rows} == {"single", "daily"}
    assert all(r["revision"] == 1 for r in rows)
    trig = [r for r in rows if r["symbol"] == "TRIG.SH"][0]
    assert trig["card_version_id"] == "cv_trig" and trig["status"] == "complete"
    snap = conn.execute(
        "SELECT input_snapshot_json FROM report_runs WHERE symbol = 'TRIG.SH'"
    ).fetchone()[0]
    assert json.loads(snap)["facts_states"]["tier_triggered"] == "triggered"

    # 同日重跑：revision 新行 + 同名文件覆盖（§9.5 降级），旧行不删
    res2 = _run(conn, tmp_path)
    assert res2.daily_revision == 2
    rows2 = conn.execute("SELECT COUNT(*) FROM report_runs").fetchone()[0]
    assert rows2 == 8
    revs = {r[0] for r in conn.execute(
        "SELECT revision FROM report_runs WHERE report_type = 'daily'")}
    assert revs == {1, 2}
    md2 = (tmp_path / "reports" / "daily" / f"{RUN_DATE}.md").read_text(encoding="utf-8")
    assert "revision 2" in md2  # 报告头记 revision 序号
    assert any("revision=2" in n for n in res2.notes)  # 日志记录


# ---------------------------------------------------------------- 卡片缺失 / 数据缺失降级（§2.5）

def test_missing_card_and_data_degraded(conn, tmp_path):
    _add_watchlist(conn, "NOCARD.SH")
    _add_bars(conn, "NOCARD.SH", WEEK)
    _add_indicator(conn, "NOCARD.SH", RUN_DATE)
    conn.commit()
    res = _run(conn, tmp_path)
    by_symbol = {s.symbol: s for s in res.symbols}
    assert by_symbol["NOCARD.SH"].status == "degraded"
    assert "no_active_card" in "；".join(by_symbol["NOCARD.SH"].reasons)
    md = (tmp_path / "reports" / "NOCARD.SH" / f"{RUN_DATE}.md").read_text(encoding="utf-8")
    assert "无 active 版本" in md and "今日无决策点" in md  # 不输出伪触发（§2.5）


# ---------------------------------------------------------------- pipeline 集成幂等（§8.1、§8.3）

def _signal_snapshot(conn):
    # anchor_id 为 DELETE+重插的内部自增关联键，重跑会重新编号（D2.1 既有语义），
    # 并会渗入 details_json（count_active_signals 输出）；幂等比对时统一剔除，
    # 其余内容列（日期/价格/阈值/原因码）重跑必须一致。
    def normalize(row: dict) -> dict:
        row = {k: v for k, v in row.items()
               if k not in ("fact_id", "created_at", "anchor_id")}
        if row.get("details_json"):
            det = json.loads(row["details_json"])
            det.pop("anchor_id", None)
            row["details_json"] = json.dumps(det, ensure_ascii=False, sort_keys=True)
        return row

    return [
        normalize(dict(r))
        for r in conn.execute(
            "SELECT * FROM signal_facts ORDER BY symbol, signal, observed_on")
    ]


def test_pipeline_signals_and_report_idempotent(conn, tmp_path):
    # 新造一只走完整 pipeline 的股（含 active 卡 → 信号阶段产出卡片信号）
    _add_watchlist(conn, "PIPE.SH")
    _add_bars(conn, "PIPE.SH", WEEK)
    _add_card(conn, "PIPE.SH", "cv_pipe",
              [{"tier": 1, "zone_low": "100.00", "zone_high": "110.00"}])
    conn.commit()
    reports = str(tmp_path / "pipe_reports")

    r1 = run_daily(conn, RUN_DATE, reports_root=reports)
    first_signals = _signal_snapshot(conn)
    first_runs = conn.execute("SELECT COUNT(*) FROM report_runs").fetchone()[0]
    pipe1 = conn.execute(
        "SELECT revision, status FROM report_runs WHERE symbol = 'PIPE.SH'"
    ).fetchone()

    r2 = run_daily(conn, RUN_DATE, reports_root=reports)
    second_signals = _signal_snapshot(conn)

    assert first_signals == second_signals  # 信号事实全链路幂等（DELETE+重插一致）
    assert r2.symbols
    # 报告文件生成且 revision 递增（§9.5 设计行为，非幂等破坏）
    assert (Path(reports) / "PIPE.SH" / f"{RUN_DATE}.md").exists()
    assert (Path(reports) / "daily" / f"{RUN_DATE}.md").exists()
    pipe2 = conn.execute(
        "SELECT MAX(revision) FROM report_runs WHERE symbol = 'PIPE.SH'").fetchone()[0]
    assert pipe1[0] == 1 and pipe2 == 2
    assert conn.execute("SELECT COUNT(*) FROM report_runs").fetchone()[0] > first_runs
    # 信号阶段 pipeline_runs 同 run_id 覆盖不膨胀
    stages = [r[0] for r in conn.execute(
        "SELECT stage FROM pipeline_runs WHERE run_id = ?", (f"daily_{RUN_DATE}",))]
    assert len(stages) == len(set(stages))
    # PIPE.SH 现价 104 进 T1 → 单股报告含档位触发决策点
    md = (Path(reports) / "PIPE.SH" / f"{RUN_DATE}.md").read_text(encoding="utf-8")
    assert "[档位触发 T1]" in md
