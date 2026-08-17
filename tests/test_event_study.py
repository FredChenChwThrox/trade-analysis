"""公告事件研究测试（设计 §5.5 保守时点）。

覆盖：
- base_date/T+k 取值语义（available_at 转 Asia/Shanghai 日期 A，base < A ≤ T+1）；
- 交易日历来自 trading_calendar（is_open 升序），index_bars 只作基准价格：
  休市日不进日历、日历缺失 → calendar_missing、指数空洞 → degraded；
- 正常事件 T+1/T+5：ret / bench_ret / excess 与手算一致（复权口径，factor 参与）；
- 停牌：开市日个股无 bar → 终点 null + suspended，不顺延；
- 数据空洞：base 日个股无 bar → degraded（不猜）；
- T+5 未到期（超出日历，或在日历内但 > 数据截止）→ null + pending、
  status='ok'；补数据后重算更新；完整行重跑跳过；
- degraded 行在数据回补后重跑被重算转正；suspended 完整行跳过，
  但 suspended 行含 pending 终点时仍按 pending 规则重算；
- 只写 assessment_version='event_study_v1'，其他版本 LLM 评价行不受影响。
"""

from __future__ import annotations

import json

import pytest

from scripts.pipeline import db
from scripts.signals import event_study as es

SYM = "TEST.SH"
# 11 个工作日（2026-01-05 周一 起），合成 trading_calendar 开市日
CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
       "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16",
       "2026-01-19"]
BASE, T1, T5 = "2026-01-09", "2026-01-12", "2026-01-16"
# 事件周六盘后发布（UTC 16:00 = 北京时间周日 00:00）→ A=2026-01-11
AVAIL = "2026-01-10T16:00:00+00:00"


# ---------------------------------------------------------------- 夹具

def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    return conn


def add_cal_day(conn, date, is_open=1):
    conn.execute(
        "INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day, "
        "status, timezone, source, updated_at) "
        "VALUES ('CN', ?, ?, 1, ?, 'Asia/Shanghai', 'test', ?)",
        (date, is_open, "trading" if is_open else "holiday", db.utc_now()),
    )


def add_index_bar(conn, date, close=1000.0):
    conn.execute(
        "INSERT INTO index_bars (index_code, trade_date, close, source, updated_at) "
        "VALUES ('000300.SH', ?, ?, 'test', ?)",
        (date, close, db.utc_now()),
    )


def add_bar(conn, date, close_raw, factor=1.0, symbol=SYM):
    conn.execute(
        "INSERT INTO daily_bars (symbol, trade_date, market, close_raw, "
        "price_adj_factor, share_factor, source, updated_at) "
        "VALUES (?, ?, 'CN', ?, ?, 1.0, 'test', ?)",
        (symbol, date, close_raw, factor, db.utc_now()),
    )


def add_event(conn, event_id="EV1", symbol=SYM, available_at=AVAIL,
              event_type="announcement"):
    conn.execute(
        "INSERT INTO events (event_id, event_type, available_at, source, "
        "ingested_at) VALUES (?, ?, ?, 'test', ?)",
        (event_id, event_type, available_at, db.utc_now()),
    )
    conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES (?, ?)",
        (event_id, symbol),
    )


def seed_bench(conn, upto=T5, start=None, skip=()):
    """000300.SH 基准指数 bar（只作价格来源，不作日历）。"""
    for d in CAL:
        if d > upto:
            break
        if start and d < start:
            continue
        if d in skip:
            continue
        add_index_bar(conn, d, {T1: 1010.0, T5: 1020.0}.get(d, 1000.0))


def seed_calendar(conn, upto=T5, start=None, closed=(), bench=True,
                  bench_skip=()):
    """trading_calendar 开市日（权威日历）+ 000300.SH 基准指数 bar。"""
    for d in CAL:
        if d > upto:
            break
        if start and d < start:
            continue
        add_cal_day(conn, d, is_open=0 if d in closed else 1)
    if bench:
        seed_bench(conn, upto=upto, start=start, skip=bench_skip)


def seed_bars(conn, upto=T5, skip=(), start=None, symbol=SYM):
    """复权收盘：base=100（50×2.0）、T+1=110（55×2.0）、T+5=120（60×2.0）。"""
    raw = {BASE: 50.0, T1: 55.0, T5: 60.0}
    for d in CAL:
        if d > upto or d in skip:
            continue
        if start and d < start:
            continue
        add_bar(conn, d, raw.get(d, 50.0), factor=2.0, symbol=symbol)


def assessment(conn, event_id="EV1", version=es.ASSESSMENT_VERSION):
    r = conn.execute(
        "SELECT * FROM event_assessments WHERE event_id = ? "
        "AND assessment_version = ?",
        (event_id, version),
    ).fetchone()
    return r


# ---------------------------------------------------------------- 纯函数：时点语义

def test_local_date_and_anchors():
    # UTC → Asia/Shanghai 日期 A；base = 日历中 < A 的最后一天
    assert es.local_date_of("2026-01-10T16:00:00+00:00") == "2026-01-11"
    assert es.local_date_of("2026-01-09T08:00:00+00:00") == "2026-01-09"
    base, t1_idx = es.anchors(CAL, "2026-01-11")   # 周日 → base 周五，T+1 周一
    assert base == BASE and CAL[t1_idx] == T1
    base, t1_idx = es.anchors(CAL, "2026-01-09")   # 盘中日发布：base 前一交易日
    assert base == "2026-01-08" and CAL[t1_idx] == "2026-01-09"
    base, t1_idx = es.anchors(CAL, "2027-01-01")   # A 超出日历 → 终点全 pending
    assert base == "2026-01-19" and t1_idx is None
    base, t1_idx = es.anchors(CAL, "2026-01-01")   # 日历无 A 之前交易日 → degraded
    assert base is None and t1_idx == 0


def test_base_date_weekend_release(tmp_path):
    """周末发布后下一开市日：base 取前一交易日（周五），T+1 为周一。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn)
        add_event(conn)
        es.run_event_study(conn)
    study = json.loads(assessment(conn)["event_study_json"])
    assert study["base_date"] == BASE
    assert study["t1"]["date"] == T1 and study["t5"]["date"] == T5
    assert study["calendar"] == "trading_calendar:CN"
    assert study["benchmark"] == "000300.SH"


def test_calendar_excludes_closed_days(tmp_path):
    """休市日不进日历（即使指数有 bar）：01-13 休市 → T+5 顺延到 01-19。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn, upto="2026-01-19", closed=("2026-01-13",))
        seed_bars(conn, upto="2026-01-19", skip=("2026-01-13",))
        add_event(conn)
        es.run_event_study(conn)
    study = json.loads(assessment(conn)["event_study_json"])
    assert assessment(conn)["status"] == "ok"
    assert study["t1"]["date"] == T1
    assert study["t5"]["date"] == "2026-01-19"   # 休市日不占交易日序号
    assert study["t5"]["ret"] == pytest.approx(0.0)  # 01-19 复权收盘 100


def test_calendar_missing_degraded(tmp_path):
    """trading_calendar 缺失 → 全部事件 degraded（calendar_missing），不拿
    指数日期冒充日历（§2.5）。"""
    conn = make_conn(tmp_path)
    with conn:
        for d in CAL:                            # 只有指数与个股 bar，无日历
            add_index_bar(conn, d, 1000.0)
        seed_bars(conn, upto="2026-01-19")
        add_event(conn)
        res = es.run_event_study(conn)
    assert res.status_counts == {"degraded": 1}
    study = json.loads(assessment(conn)["event_study_json"])
    assert study["reason"] == "calendar_missing"
    assert study["base_date"] is None and study["t1"]["mark"] is None


def test_bench_hole_degraded(tmp_path):
    """开市日指数无 bar（数据空洞）→ degraded，不静默顺延；个股收盘照实填。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn, bench_skip=(T1,))    # T+1 日指数 bar 缺失
        seed_bars(conn)
        add_event(conn)
        es.run_event_study(conn)
    row = assessment(conn)
    assert row["status"] == "degraded"
    study = json.loads(row["event_study_json"])
    assert study["reason"] == "bench_missing_t1"
    t1 = study["t1"]
    assert t1["mark"] is None and t1["date"] == T1
    assert t1["close"] == 110.0 and t1["ret"] == pytest.approx(0.10)
    assert t1["bench_ret"] is None and t1["excess"] is None
    assert study["t5"]["ret"] == pytest.approx(0.20)  # t5 基准齐全照算
    assert study["t5"]["excess"] == pytest.approx(0.18)


# ---------------------------------------------------------------- 正常事件手算

def test_normal_event_hand_calculated(tmp_path):
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn)
        add_event(conn)
        res = es.run_event_study(conn, run_id="run_normal")
    row = assessment(conn)
    assert row["status"] == "ok"
    assert row["model"] == "deterministic"
    assert row["assessment_version"] == "event_study_v1"
    assert row["event_type"] == "announcement"
    # 不冒充 LLM 评价（§5.5）
    for col in ("prompt_version", "direction", "materiality", "confidence",
                "rationale"):
        assert row[col] is None
    study = json.loads(row["event_study_json"])
    assert study["base_close"] == 100.0          # 50 × 2.0 复权口径
    t1, t5 = study["t1"], study["t5"]
    assert t1["close"] == 110.0 and t1["mark"] is None
    assert t1["ret"] == pytest.approx(0.10)       # 110/100 − 1
    assert t1["bench_ret"] == pytest.approx(0.01)  # 1010/1000 − 1
    assert t1["excess"] == pytest.approx(0.09)
    assert t5["close"] == 120.0
    assert t5["ret"] == pytest.approx(0.20)
    assert t5["bench_ret"] == pytest.approx(0.02)  # 1020/1000 − 1
    assert t5["excess"] == pytest.approx(0.18)
    assert study["run_id"] == "run_normal"
    run = conn.execute(
        "SELECT stage, status, adapter_version FROM pipeline_runs "
        "WHERE run_id = 'run_normal'").fetchone()
    assert run["stage"] == "event_study" and run["status"] == "success"
    assert run["adapter_version"] is None


# ---------------------------------------------------------------- 停牌

def test_suspended_t1(tmp_path):
    """T+1 日个股无 bar → 终点 null + mark=suspended，不顺延冒充；T+5 照算。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn, skip=(T1,))
        add_event(conn)
        es.run_event_study(conn)
    row = assessment(conn)
    assert row["status"] == "suspended"
    study = json.loads(row["event_study_json"])
    t1 = study["t1"]
    assert t1["mark"] == "suspended" and t1["date"] == T1
    assert t1["close"] is None and t1["ret"] is None and t1["excess"] is None
    assert study["t5"]["mark"] is None and study["t5"]["ret"] == pytest.approx(0.20)


# ---------------------------------------------------------------- 数据空洞

def test_degraded_base_no_bar(tmp_path):
    """base 日个股无 bar → degraded（不猜），终点全 null。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn, skip=(BASE,))
        add_event(conn)
        es.run_event_study(conn)
    row = assessment(conn)
    assert row["status"] == "degraded"
    study = json.loads(row["event_study_json"])
    assert study["reason"] == "base_no_bar"
    assert study["base_date"] == BASE and study["base_close"] is None
    for name in ("t1", "t5"):
        assert study[name]["mark"] is None and study[name]["ret"] is None


def test_degraded_no_base_date(tmp_path):
    """日历本身无 A 之前的交易日 → degraded。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn)
        add_event(conn, available_at="2026-01-01T16:00:00+00:00")  # A=01-02 早于日历
        es.run_event_study(conn)
    study = json.loads(assessment(conn)["event_study_json"])
    assert assessment(conn)["status"] == "degraded"
    assert study["reason"] == "no_base_date" and study["base_date"] is None


# ---------------------------------------------------------------- pending 与幂等

def test_future_endpoint_pending_not_suspended(tmp_path):
    """终点日在日历内但 > 数据截止 → pending 而非 suspended（复现 08-12 公告
    t5 未到期场景）；数据补齐后重算转正。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn, upto="2026-01-19", bench=False)  # 权威日历到 01-19
        seed_bench(conn, upto="2026-01-14")    # 数据（指数与个股）只到 01-14
        seed_bars(conn, upto="2026-01-14")     # 数据截止 = 01-14
        # A=2026-01-13：base=01-12，t1=01-13（≤截止，可算），t5=01-19（>截止）
        add_event(conn, available_at="2026-01-12T16:00:00+00:00")
        r1 = es.run_event_study(conn, run_id="run_f1")
    assert r1.status_counts == {"ok": 1}       # 只有 pending 不算错误
    study = json.loads(assessment(conn)["event_study_json"])
    assert study["base_date"] == T1 and study["base_close"] == 110.0
    assert study["t1"]["date"] == "2026-01-13" and study["t1"]["mark"] is None
    t5 = study["t5"]
    assert t5["mark"] == "pending" and t5["date"] == "2026-01-19"  # 未到期非停牌
    assert t5["close"] is None and t5["ret"] is None

    with conn:
        seed_bench(conn, upto="2026-01-19", start="2026-01-15")  # 数据补到 01-19
        seed_bars(conn, upto="2026-01-19", start="2026-01-15")
        r2 = es.run_event_study(conn, run_id="run_f2")
    assert r2.recomputed == 1 and r2.skipped == 0
    study = json.loads(assessment(conn)["event_study_json"])
    assert study["t5"]["mark"] is None
    assert study["t5"]["ret"] == pytest.approx(100 / 110 - 1)  # 100/110−1
    assert assessment(conn)["status"] == "ok"


def test_pending_then_recompute_then_skip(tmp_path):
    """T+5 未到期 → null+pending、status='ok'；补数据后重算更新；完整行跳过。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn, upto="2026-01-14")   # 日历只到 T+3
        seed_bars(conn, upto="2026-01-14")
        add_event(conn)
        r1 = es.run_event_study(conn, run_id="run_p1")
    assert r1.written == 1 and r1.status_counts == {"ok": 1}
    row = assessment(conn)
    assert row["status"] == "ok"                 # 只有 pending 不算错误
    study = json.loads(row["event_study_json"])
    assert study["t1"]["ret"] == pytest.approx(0.10)   # T+1 能算先算
    t5 = study["t5"]
    assert t5["mark"] == "pending" and t5["date"] is None and t5["ret"] is None

    with conn:
        seed_calendar(conn, start="2026-01-15")   # 补上 01-15/01-16 指数与个股 bar
        seed_bars(conn, start="2026-01-15")
        r2 = es.run_event_study(conn, run_id="run_p2")
    assert r2.recomputed == 1 and r2.written == 1
    row = assessment(conn)
    assert row["status"] == "ok"
    study = json.loads(row["event_study_json"])
    assert study["t5"]["mark"] is None and study["t5"]["ret"] == pytest.approx(0.20)
    assessed_at = row["assessed_at"]

    with conn:
        r3 = es.run_event_study(conn, run_id="run_p3")   # 完整行重跑 → 跳过
    assert r3.skipped == 1 and r3.written == 0
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM event_assessments "
        "WHERE assessment_version = 'event_study_v1'").fetchone()["c"]
    assert n == 1                                  # 插入计数不变
    assert assessment(conn)["assessed_at"] == assessed_at  # 未重算


# ---------------------------------------------------------------- degraded 重算

def test_degraded_recomputed_after_backfill(tmp_path):
    """degraded（no_base_date）行在日历回补后重跑被重算转正（不被跳过）。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn, start="2026-01-12")  # 日历从 T+1 起，无 base 交易日
        seed_bars(conn, start="2026-01-12")
        add_event(conn)                          # A=2026-01-11
        r1 = es.run_event_study(conn, run_id="run_d1")
    assert assessment(conn)["status"] == "degraded"
    assert json.loads(assessment(conn)["event_study_json"])["reason"] == "no_base_date"

    with conn:
        seed_calendar(conn, upto=BASE)           # 回补 01-05..01-09 指数与个股 bar
        seed_bars(conn, upto=BASE)
        r2 = es.run_event_study(conn, run_id="run_d2")
    assert r1.written == 1 and r2.recomputed == 1 and r2.written == 1
    row = assessment(conn)
    assert row["status"] == "ok"                 # 转正
    study = json.loads(row["event_study_json"])
    assert study["reason"] is None and study["base_date"] == BASE
    assert study["t1"]["ret"] == pytest.approx(0.10)
    assert study["t5"]["ret"] == pytest.approx(0.20)

    with conn:
        r3 = es.run_event_study(conn, run_id="run_d3")   # 转正后完整行 → 跳过
    assert r3.skipped == 1 and r3.written == 0


def test_suspended_complete_row_skipped(tmp_path):
    """suspended 且无 pending 是完整事实结果（停牌历史不变）→ 重跑跳过。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn, skip=(T1,))
        add_event(conn)
        es.run_event_study(conn, run_id="run_s1")
    assert assessment(conn)["status"] == "suspended"
    assessed_at = assessment(conn)["assessed_at"]
    with conn:
        r2 = es.run_event_study(conn, run_id="run_s2")
    assert r2.skipped == 1 and r2.written == 0 and r2.recomputed == 0
    assert assessment(conn)["assessed_at"] == assessed_at  # 未重算


def test_suspended_with_pending_recomputed(tmp_path):
    """suspended 行另一终点 pending → 仍按 pending 规则重算。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn, upto="2026-01-14")   # 日历只到 T+3 → t5 pending
        seed_bars(conn, upto="2026-01-14", skip=(T1,))  # t1 停牌
        add_event(conn)
        r1 = es.run_event_study(conn, run_id="run_sp1")
    assert r1.status_counts == {"suspended": 1}
    study = json.loads(assessment(conn)["event_study_json"])
    assert study["t1"]["mark"] == "suspended" and study["t5"]["mark"] == "pending"

    with conn:
        r2 = es.run_event_study(conn, run_id="run_sp2")  # 含 pending → 重算
    assert r2.recomputed == 1 and r2.skipped == 0
    study = json.loads(assessment(conn)["event_study_json"])
    assert study["t1"]["mark"] == "suspended"    # 停牌事实不变

    with conn:
        seed_calendar(conn, start="2026-01-15")  # 补 T+5 数据后重算 → 终点齐全
        seed_bars(conn, start="2026-01-15")
        r3 = es.run_event_study(conn, run_id="run_sp3")
    assert r3.recomputed == 1
    study = json.loads(assessment(conn)["event_study_json"])
    assert study["t5"]["mark"] is None and study["t5"]["ret"] == pytest.approx(0.20)
    assert assessment(conn)["status"] == "suspended"  # t1 仍停牌


# ---------------------------------------------------------------- 版本隔离

def test_other_assessment_version_untouched(tmp_path):
    """预置其他 version 的 LLM 评价行不受事件研究影响。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn)
        add_event(conn)
        conn.execute(
            "INSERT INTO event_assessments (event_id, assessment_version, model, "
            "prompt_version, assessed_at, direction, materiality, confidence, "
            "rationale, status) VALUES ('EV1', 7, 'llm-x', 'p3', ?, 'positive', "
            "'high', 0.9, 'keep me', 'ok')",
            (db.utc_now(),),
        )
        es.run_event_study(conn)
    llm = assessment(conn, version=7)
    assert llm["model"] == "llm-x" and llm["rationale"] == "keep me"
    assert llm["event_study_json"] is None
    assert assessment(conn)["status"] == "ok"      # v1 行并存
    n = conn.execute("SELECT COUNT(*) AS c FROM event_assessments").fetchone()["c"]
    assert n == 2


def test_symbol_filter(tmp_path):
    """--symbol 只处理该 symbol 的公告事件。"""
    conn = make_conn(tmp_path)
    with conn:
        seed_calendar(conn)
        seed_bars(conn)
        seed_bars(conn, symbol="OTHER.SH")
        add_event(conn, event_id="EV1", symbol=SYM)
        add_event(conn, event_id="EV2", symbol="OTHER.SH")
        res = es.run_event_study(conn, symbol=SYM)
    assert res.events_seen == 1 and res.written == 1
    assert assessment(conn, "EV1") is not None
    assert assessment(conn, "EV2") is None
