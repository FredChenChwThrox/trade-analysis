"""agent/skill 打标通道测试（消息面 r2 Phase 3）。

锁定：export 底稿格式、候选过滤（available_at 点时、--symbol 个股过滤）、
import schema 校验（非法拒绝不冒充）、事件事实以 events 表为准（tier 取库值）、
gate（company+tier1 → needs_review、banned word → needs_review）、
narratives 逐股行、已评价跳过、6c 关联自动执行。
"""

from __future__ import annotations

import json

from scripts.llm import inputs as inputs_mod
from scripts.pipeline import db as pdb


def _mkconn(tmp_path):
    conn = pdb.connect(tmp_path / "t.db")
    pdb.migrate(conn)
    now = pdb.utc_now()
    conn.execute(
        "INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,"
        " currency, timezone, active, created_at, updated_at)"
        " VALUES ('601899.SH', 'CN', '紫金矿业', '[]', '000300.SH', 'CNY',"
        " 'Asia/Shanghai', 1, ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO events (event_id, event_type, published_at, published_tz,"
        " available_at, title, summary, source, source_tier, scope, content_hash,"
        " ingested_at) VALUES"
        " ('evt_a', 'announcement', '2026-08-28T03:00:00+00:00', 'Asia/Shanghai',"
        "  '2026-08-28T03:00:00+00:00', '公司回购公告', NULL, 'akshare', 1, NULL,"
        "  'h1', ?)", (now,))
    conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES ('evt_a', '601899.SH')")
    conn.commit()
    return conn


def test_export_import_roundtrip(tmp_path):
    conn = _mkconn(tmp_path)
    out = tmp_path / "input.jsonl"
    assert inputs_mod.export_inputs(conn, "2026-08-28", 10, out) == 1
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["event_id"] == "evt_a" and rec["event_type"] == "announcement"

    # --symbol 过滤：别的股票 → 0 条；本股 → 1 条
    assert inputs_mod.export_inputs(conn, "2026-08-28", 10,
                                    tmp_path / "other.jsonl",
                                    symbol="600029.SH") == 0
    assert inputs_mod.export_inputs(conn, "2026-08-28", 10,
                                    tmp_path / "same.jsonl",
                                    symbol="601899.SH") == 1

    # agent 标签：不写 event_type/source_tier（以 events 表为准）
    tags = tmp_path / "tags.jsonl"
    tags.write_text(json.dumps({
        "event_id": "evt_a", "scope": "company", "direction": "positive",
        "materiality": "medium", "confidence": 0.8, "target": "eps",
        "half_life": "month", "expectation_gap": None, "action_hint": "none",
        "falsification_suggestion": None, "rationale": "回购减少股本",
        "narratives": [{"symbol": "601899.SH", "narrative": "回购利好股本结构"}],
    }, ensure_ascii=False), encoding="utf-8")

    stats = inputs_mod.import_tags(conn, tags, actor="claude", as_of="2026-08-28")
    assert stats["accepted"] == 1 and stats["rejected"] == 0
    ev = conn.execute(
        "SELECT * FROM event_assessments WHERE event_id='evt_a' "
        "AND symbol='__event__'").fetchone()
    assert ev["model"] == "agent:claude"
    assert ev["event_type"] == "announcement"      # 事实取 events 表
    assert ev["status"] == "needs_review"          # company+tier1 强制人审
    nar = conn.execute(
        "SELECT narrative FROM event_assessments WHERE event_id='evt_a' "
        "AND symbol='601899.SH'").fetchone()
    assert nar["narrative"] == "回购利好股本结构"
    assert conn.execute("SELECT scope FROM events WHERE event_id='evt_a'"
                        ).fetchone()[0] == "company"

    # 已评价跳过
    stats2 = inputs_mod.import_tags(conn, tags, actor="claude", as_of="2026-08-28")
    assert stats2["skipped"] == 1 and stats2["accepted"] == 0
    conn.close()


def test_import_rejects_invalid_tag(tmp_path):
    conn = _mkconn(tmp_path)
    tags = tmp_path / "bad.jsonl"
    tags.write_text(json.dumps({
        "event_id": "evt_a", "scope": "company", "direction": "暴涨",
        "materiality": "medium", "confidence": 0.8, "action_hint": "none",
        "rationale": "r",
    }, ensure_ascii=False), encoding="utf-8")
    stats = inputs_mod.import_tags(conn, tags, actor="claude", as_of="2026-08-28")
    assert stats["rejected"] == 1 and stats["accepted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_assessments").fetchone()[0] == 0
    conn.close()


def test_gate_rules():
    gate = inputs_mod.gate
    review_gate = {"high_materiality": ["high", "critical"],
                   "low_confidence": 0.4, "company_tier_max": 2,
                   "banned_words": ["必涨"]}
    base = {"materiality": "medium", "confidence": 0.8, "rationale": "库存下行"}
    assert gate(review_gate, "industry", 4, base) == "ok"
    assert gate(review_gate, "company", 1, base) == "needs_review"   # 强制人审
    assert gate(review_gate, "company", 4, base) == "ok"             # 非公司层不限
    assert gate(review_gate, "industry", 4,
                {**base, "materiality": "high"}) == "needs_review"
    assert gate(review_gate, "industry", 4,
                {**base, "confidence": 0.3}) == "needs_review"
    assert gate(review_gate, "industry", 4,
                {**base, "rationale": "此事件后必涨"}) == "needs_review"
