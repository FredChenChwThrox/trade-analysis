"""agent/skill 打标通道（消息面 r2 Phase 3，与 API 通道产出同一 llm_v1 行）。

用法（配合 skills/message-tag-skill/SKILL.md）：
    # 1. 导出未评价事件底稿（JSONL）
    uv run python -m scripts.llm.inputs export --as-of 2026-08-28 --limit 20
    #    → data/llm_tags/2026-08-28_input.jsonl
    # 2. agent（skill）逐条打标 → tags JSONL（schema 同 scripts/llm/schema.py，
    #    另加可选 "narratives": [{"symbol": ..., "narrative": ...}]）
    # 3. 导入：事件事实（event_type/source_tier）以 events 表为准；
    #    schema 校验（非法行拒绝不冒充）→ gate（r2 §6.3）→ llm_v1 行 → 6c 关联
    uv run python -m scripts.llm.inputs import --tags data/llm_tags/tags.jsonl \\
        --actor claude --as-of 2026-08-28

导入语义：已评价（llm_v1 存在）跳过；gate 与 API 通道一致（needs_review 进
/message-review 人审）；model 记 `agent:<actor>` 可追溯打标主体。
"""

from __future__ import annotations

import argparse
import json
import jsonschema
import sqlite3
import sys
from pathlib import Path

from scripts.llm import schema
from scripts.llm.client import load_config
from scripts.llm.eval import gate
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals import event_link

CANDIDATE_SQL = """
SELECT e.event_id, e.event_type, e.title, e.summary, e.published_at,
       e.source, e.source_tier
FROM events e
WHERE e.event_type IN ('announcement', 'news')
  AND e.available_at <= ?
  AND NOT EXISTS (SELECT 1 FROM event_assessments a
                  WHERE a.event_id = e.event_id AND a.symbol = '__event__'
                    AND a.assessment_version = 'llm_v1')
ORDER BY e.published_at DESC
LIMIT ?
"""


def export_inputs(conn: sqlite3.Connection, as_of: str, limit: int,
                  out: Path) -> int:
    """导出未评价事件底稿 JSONL（事件字段 + 宏观因子快照行）。"""
    macro_lines = [r["line"] for r in conn.execute(
        """
        SELECT code || ' ' || name || ' ' || close || COALESCE(unit, '')
               || '（' || trade_date || '）' AS line
        FROM (SELECT code, name, close, unit, trade_date, MAX(trade_date)
              FROM macro_factors WHERE trade_date <= ? GROUP BY code)
        ORDER BY code
        """, (as_of,))]
    rows = conn.execute(CANDIDATE_SQL, (f"{as_of}T23:59:59+00:00", limit)).fetchall()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "event_id": r["event_id"], "event_type": r["event_type"],
                "title": r["title"], "summary": r["summary"],
                "published_at": r["published_at"], "source": r["source"],
                "source_tier": r["source_tier"],
                "macro_context": macro_lines,
            }, ensure_ascii=False) + "\n")
    return len(rows)


def import_tags(conn: sqlite3.Connection, tags_path: Path, *, actor: str,
                as_of: str, cfg=None) -> dict:
    """tags JSONL → 校验（非法行拒绝不冒充）→ gate → llm_v1 行 → 6c 关联。

    model 记 `agent:<actor>`（可追溯打标主体，区别于 API 通道的模型名）；
    event_type / source_tier 以 events 表事实为准（不信任标签行）。
    """
    cfg = cfg or load_config()
    accepted = rejected = skipped = 0
    notes: list[str] = []
    for line in Path(tags_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            tag = json.loads(line)
            event_id = tag["event_id"]
        except (json.JSONDecodeError, KeyError) as exc:
            rejected += 1
            notes.append(f"行解析失败: {exc}")
            continue
        ev = conn.execute(
            "SELECT event_type, source_tier FROM events WHERE event_id = ?",
            (event_id,)).fetchone()
        if ev is None:
            rejected += 1
            notes.append(f"{event_id} 事件不存在")
            continue
        exists = conn.execute(
            "SELECT 1 FROM event_assessments WHERE event_id = ? AND symbol = '__event__' "
            "AND assessment_version = 'llm_v1'", (event_id,)).fetchone()
        if exists:
            skipped += 1
            continue
        try:
            schema.validate_event(schema.normalize_event(tag))
        except jsonschema.ValidationError as exc:
            rejected += 1
            notes.append(f"{event_id} 校验拒绝: {exc.message[:120]}")
            continue
        narratives = tag.get("narratives") or []
        status = gate(cfg, tag["scope"], ev["source_tier"], tag)
        now = utc_now()
        conn.execute(
            """
            INSERT OR REPLACE INTO event_assessments (
                event_id, symbol, assessment_version, model, prompt_version,
                assessed_at, event_type, direction, materiality, confidence,
                rationale, target, half_life, expectation_gap, action_hint,
                falsification, status, run_id)
            VALUES (?, '__event__', 'llm_v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, f"agent:{actor}", cfg.prompt_version, now,
             ev["event_type"], tag["direction"], tag["materiality"],
             tag["confidence"], tag["rationale"], tag.get("target"),
             tag.get("half_life"), tag.get("expectation_gap"),
             tag["action_hint"], tag.get("falsification_suggestion"),
             status, f"llm_import_{as_of}"))
        conn.execute("UPDATE events SET scope = ? WHERE event_id = ?",
                     (tag["scope"], event_id))
        for n in narratives:
            if not (n.get("symbol") and n.get("narrative")):
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO event_assessments (
                    event_id, symbol, assessment_version, model, prompt_version,
                    assessed_at, event_type, direction, materiality, confidence,
                    rationale, status, narrative, run_id)
                VALUES (?, ?, 'llm_v1', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
                """,
                (event_id, n["symbol"], f"agent:{actor}", cfg.prompt_version,
                 now, status, n["narrative"], f"llm_import_{as_of}"))
        accepted += 1
    link_stats = event_link.run_link_stage(conn)
    return {"accepted": accepted, "rejected": rejected, "skipped": skipped,
            "links_added": link_stats["links_added"],
            "scope_updated": link_stats["scope_updated"], "notes": notes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.llm.inputs")
    parser.add_argument("command", choices=["export", "import"])
    parser.add_argument("--as-of", required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", default=None, help="export：底稿输出路径")
    parser.add_argument("--tags", default=None, help="import：标签 JSONL 路径")
    parser.add_argument("--actor", default="agent",
                        help="import：打标主体标识（model 记 agent:<actor>）")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        if args.command == "export":
            out = Path(args.out or f"data/llm_tags/{args.as_of}_input.jsonl")
            n = export_inputs(conn, args.as_of, args.limit, out)
            print(f"[export] {n} 条底稿 → {out}")
        else:
            if not args.tags:
                parser.error("import 需要 --tags")
            stats = import_tags(conn, Path(args.tags), actor=args.actor,
                                as_of=args.as_of)
            print(f"[import] 接受 {stats['accepted']}，拒绝 {stats['rejected']}，"
                  f"跳过(已评) {stats['skipped']}；关联 +{stats['links_added']}，"
                  f"scope 更新 {stats['scope_updated']}")
            for note in stats["notes"][:10]:
                print(f"  {note}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
