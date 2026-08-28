"""LLM 评价链编排测试（消息面 r2 Phase 3）——FakeLLM，不发真实请求。

锁定：disabled 路径（enabled=false）；6b1 写 __event__ llm_v1 行 + scope 回填 +
gate（company tier1→needs_review、banned word→needs_review）；JSON 非法丢弃不冒充
（stage degraded）；6b2 逐股叙事行；成功评价幂等不重调、失败事件保留候选可重试
（r2 §9：可有限重试，仍失败记 degraded）。
"""

from __future__ import annotations

import pytest

from scripts.llm import schema
from scripts.llm.client import LLMConfig, LLMError
from scripts.llm.eval import run_llm_eval
from scripts.pipeline import db as pdb


def _cfg(**kw) -> LLMConfig:
    base = dict(enabled=True, base_url="http://x", model="fake", api_key_env="X",
                temperature=0.1, timeout_seconds=1, max_retries=1, batch_size=20,
                max_concurrency=1, max_llm_calls_per_run=100,
                review_gate={"high_materiality": ["high", "critical"],
                             "low_confidence": 0.4, "company_tier_max": 2,
                             "banned_words": ["必涨"]},
                prompt_version="llm_v1")
    base.update(kw)
    return LLMConfig(**base)


class FakeClient:
    """按序返回预设结果；Exception 抛出模拟调用失败。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[tuple[str, str]] = []

    def available(self):
        return True

    def complete_json(self, system, user):
        self.calls.append((system, user))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _mkconn(tmp_path):
    conn = pdb.connect(tmp_path / "t.db")
    pdb.migrate(conn)
    now = pdb.utc_now()
    conn.execute(
        "INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,"
        " currency, timezone, active, created_at, updated_at)"
        " VALUES ('601899.SH', 'CN', '紫金矿业', '[]', '000300.SH', 'CNY',"
        " 'Asia/Shanghai', 1, ?, ?)", (now, now))
    conn.execute("UPDATE watchlist SET themes_json = ? WHERE symbol = '601899.SH'",
                 ('["铜"]',))
    conn.execute(
        "INSERT INTO macro_factors (factor_type, code, name, market, trade_date,"
        " close, change_pct, unit, source, ingested_at)"
        " VALUES ('commodity', 'CU0', '沪铜', 'CN', '2026-08-28', '108900.0',"
        " NULL, '元/吨', 'akshare', ?)", (now,))
    # published_at 互不相同 → 6b1 候选顺序确定（DESC：evt_bad → evt_gate → evt_ok）
    conn.execute(
        "INSERT INTO events (event_id, event_type, published_at, published_tz,"
        " available_at, title, summary, source, source_tier, scope, content_hash,"
        " ingested_at) VALUES"
        " ('evt_ok', 'news', '2026-08-28T02:00:00+00:00', 'Asia/Shanghai',"
        "  '2026-08-28T02:00:00+00:00', 'LME铜 库存大降 5万吨', NULL, 'akshare',"
        "  4, NULL, 'h1', ?),"
        " ('evt_gate', 'announcement', '2026-08-28T03:00:00+00:00',"
        "  'Asia/Shanghai', '2026-08-28T03:00:00+00:00', '公司回购公告',"
        "  NULL, 'akshare', 1, NULL, 'h2', ?),"
        " ('evt_bad', 'news', '2026-08-28T04:00:00+00:00', 'Asia/Shanghai',"
        "  '2026-08-28T04:00:00+00:00', '含禁词事件', NULL, 'akshare', 4, NULL,"
        "  'h3', ?)", (now, now, now))
    conn.commit()
    return conn


_OK_EVENT = {"scope": "industry", "direction": "negative", "materiality": "medium",
             "confidence": 0.75, "target": "eps", "half_life": "week",
             "expectation_gap": None, "action_hint": "none",
             "falsification_suggestion": None, "rationale": "库存下行支撑价格"}
_OK_NARR = {"narrative": "铜价走弱或压制矿业利润，观察季度售价"}
_GATE_EVENT = {"scope": "company", "direction": "positive", "materiality": "medium",
               "confidence": 0.8, "target": "eps", "half_life": "month",
               "expectation_gap": None, "action_hint": "none",
               "falsification_suggestion": None, "rationale": "回购减少股本"}


def test_run_llm_eval_disabled(tmp_path):
    conn = _mkconn(tmp_path)
    res = run_llm_eval(conn, run_id="r", as_of="2026-08-28T23:59:59+00:00",
                       cfg=_cfg(enabled=False))
    assert res.status == "disabled" and res.assessed == 0
    conn.close()


def test_run_llm_eval_full_chain(tmp_path):
    conn = _mkconn(tmp_path)
    client = FakeClient([
        LLMError("bad json"),   # evt_bad（published_at 最新，DESC 首位）→ 丢弃
        dict(_GATE_EVENT),      # evt_gate（company+tier1 → needs_review）
        dict(_OK_EVENT),        # evt_ok → ok；6c themes 关联 601899
        dict(_OK_NARR),         # 6b2 叙事（evt_ok × 601899）
    ])
    res = run_llm_eval(conn, run_id="r1", as_of="2026-08-28T23:59:59+00:00",
                       client=client, cfg=_cfg())
    assert res.status == "degraded" and res.skipped == 1
    assert res.assessed == 2 and res.narratives == 1
    rows = conn.execute(
        "SELECT * FROM event_assessments WHERE assessment_version = 'llm_v1' "
        "ORDER BY event_id, symbol").fetchall()
    by = {(r["event_id"], r["symbol"]): r for r in rows}
    assert ("evt_ok", "__event__") in by and ("evt_gate", "__event__") in by
    assert ("evt_bad", "__event__") not in by                # 丢弃不冒充
    assert by[("evt_gate", "__event__")]["status"] == "needs_review"  # company tier1
    assert by[("evt_ok", "__event__")]["status"] == "ok"
    assert ("evt_ok", "601899.SH") in by                     # 6b2 叙事行
    assert by[("evt_ok", "601899.SH")]["narrative"].startswith("铜价")
    # 6b1 回填 scope + 6c 关联
    assert conn.execute("SELECT scope FROM events WHERE event_id='evt_ok'"
                        ).fetchone()[0] == "industry"
    assert conn.execute("SELECT COUNT(*) FROM event_symbols WHERE event_id='evt_ok'"
                        ).fetchone()[0] >= 1

    # 重试语义（r2 §9）：成功评价过的事件不重调；失败事件（evt_bad）保留候选被重试。
    # 重试返回 _OK_EVENT（scope=industry），"含禁词事件"文本不含"铜"→ 无关联无叙事。
    client2 = FakeClient([dict(_OK_EVENT)])
    res2 = run_llm_eval(conn, run_id="r2", as_of="2026-08-28T23:59:59+00:00",
                        client=client2, cfg=_cfg())
    assert res2.assessed == 1 and res2.narratives == 0 and res2.status == "ok"
    retried = [u for _, u in client2.calls]
    assert len(retried) == 1 and "含禁词事件" in retried[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM event_assessments WHERE assessment_version='llm_v1'"
        ).fetchone()[0] == 4  # 3 事件级 + evt_ok×601899 叙事

    # 全部评价完毕 → 第三轮零调用（幂等）
    client3 = FakeClient([])
    res3 = run_llm_eval(conn, run_id="r3", as_of="2026-08-28T23:59:59+00:00",
                        client=client3, cfg=_cfg())
    assert res3.assessed == 0 and res3.narratives == 0 and len(client3.calls) == 0
    conn.close()


def test_gate_banned_word(tmp_path):
    conn = _mkconn(tmp_path)
    conn.execute("DELETE FROM events WHERE event_id != 'evt_bad'")  # 单候选，消序
    client = FakeClient([{"scope": "macro", "direction": "positive",
                          "materiality": "low", "confidence": 0.9, "target": None,
                          "half_life": None, "expectation_gap": None,
                          "action_hint": "none", "falsification_suggestion": None,
                          "rationale": "此事件后必涨"}])
    res = run_llm_eval(conn, run_id="r", as_of="2026-08-28T23:59:59+00:00",
                       client=client, cfg=_cfg())
    row = conn.execute(
        "SELECT status FROM event_assessments WHERE event_id='evt_bad' "
        "AND symbol='__event__'").fetchone()
    assert row["status"] == "needs_review" and res.assessed == 1  # 禁用词→人审
    conn.close()


def test_output_schema_validation():
    schema.validate_event(dict(_OK_EVENT))
    with pytest.raises(Exception):
        schema.validate_event({**_OK_EVENT, "materiality": "暴涨"})   # 枚举非法
    with pytest.raises(Exception):
        schema.validate_event({**_OK_EVENT, "rationale": "x" * 301})  # 超长
    with pytest.raises(Exception):
        schema.validate_event({**_OK_EVENT, "extra": "x"})            # 未知字段
