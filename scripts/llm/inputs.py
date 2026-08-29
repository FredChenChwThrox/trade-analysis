"""agent/skill 打标通道（消息面 r2 Phase 3，产出 llm_v1 行）。

用法（配合 skills/message-tag-skill/SKILL.md）：
    # 1. 导出未评价事件底稿（JSONL）。默认全池最新 N 条；--symbol 可只看个股。
    uv run python -m scripts.llm.inputs export --as-of 2026-08-28 --limit 20
    uv run python -m scripts.llm.inputs export --as-of 2026-08-28 --symbol 601088.SH
    #    → data/llm_tags/<as-of>[_<symbol>]_input.jsonl
    # 2. agent（skill）逐条打标 → tags JSONL（schema 同 scripts/llm/schema.py，
    #    另加可选 "narratives": [{"symbol": ..., "narrative": ...}]）
    # 3. 导入：事件事实（event_type/source_tier）以 events 表为准；
    #    schema 校验（非法行拒绝不冒充）→ gate（r2 §6.3）→ llm_v1 行 → 6c 关联
    uv run python -m scripts.llm.inputs import --tags data/llm_tags/tags.jsonl \\
        --actor claude --as-of 2026-08-28

导入语义：已评价（llm_v1 存在）跳过；gate 与设计一致（needs_review 进
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
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals import event_link

ROOT = Path(__file__).resolve().parents[2]
LLM_CONFIG = ROOT / "config" / "llm.yaml"

CANDIDATE_SQL = """
SELECT e.event_id, e.event_type, e.title, e.summary, e.published_at,
       e.source, e.source_tier
FROM events e
WHERE e.event_type IN ('announcement', 'news')
  AND e.available_at <= ?
  AND NOT EXISTS (SELECT 1 FROM event_assessments a
                  WHERE a.event_id = e.event_id AND a.symbol = '__event__'
                    AND a.assessment_version = 'llm_v1')
  {symbol_filter}
ORDER BY e.published_at DESC
LIMIT ?
"""
_SYMBOL_FILTER = (
    " AND EXISTS (SELECT 1 FROM event_symbols es "
    "WHERE es.event_id = e.event_id AND es.symbol = ?) ")

# gate 参数（r2 §6.3）读 config/llm.yaml 的 review_gate；prompt_version 同文件。


def _llm_settings() -> tuple[dict, str]:
    import yaml

    doc = yaml.safe_load(LLM_CONFIG.read_text(encoding="utf-8"))
    return (doc.get("review_gate") or {},
            str(doc.get("prompt_version", "llm_v1")))


def gate(review_gate: dict, scope: str, source_tier: int | None, result: dict) -> str:
    """混合人审 gate（r2 §6.3）：命中任一 → needs_review，否则 ok。"""
    if result["materiality"] in set(review_gate.get("high_materiality") or []):
        return "needs_review"
    if float(result["confidence"]) < float(review_gate.get("low_confidence", 0.4)):
        return "needs_review"
    rationale = result["rationale"]
    if any(word in rationale for word in (review_gate.get("banned_words") or [])):
        return "needs_review"
    if (scope == "company" and source_tier is not None
            and source_tier <= int(review_gate.get("company_tier_max", 2))):
        return "needs_review"
    return "ok"


def export_inputs(conn: sqlite3.Connection, as_of: str, limit: int, out: Path,
                  symbol: str | None = None) -> int:
    """导出未评价事件底稿 JSONL（事件字段 + 宏观因子快照行）。

    symbol 给定时只导出与该股关联（event_symbols）的未评价事件——个股深查模式；
    缺省为全池最新 N 条。宏观因子背景为全市场快照（两种模式都带）。
    """
    import yaml

    macro_lines = [r["line"] for r in conn.execute(
        """
        SELECT code || ' ' || name || ' ' || close || COALESCE(unit, '')
               || '（' || trade_date || '）' AS line
        FROM (SELECT code, name, close, unit, trade_date, MAX(trade_date)
              FROM macro_factors WHERE trade_date <= ? GROUP BY code)
        ORDER BY code
        """, (as_of,))]
    sql = CANDIDATE_SQL.format(
        symbol_filter=_SYMBOL_FILTER if symbol else "")
    params: list = [f"{as_of}T23:59:59+00:00"]
    if symbol:
        params.append(symbol)
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
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
                as_of: str, review_gate: dict | None = None,
                prompt_version: str = "llm_v1") -> dict:
    """tags JSONL → 校验（非法行拒绝不冒充）→ gate → llm_v1 行 → 6c 关联。

    model 记 `agent:<actor>`（可追溯打标主体）；event_type / source_tier 以
    events 表事实为准（不信任标签行）。
    """
    review_gate = review_gate if review_gate is not None else _llm_settings()[0]
    prompt_version = prompt_version or _llm_settings()[1]
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
        status = gate(review_gate, tag["scope"], ev["source_tier"], tag)
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
            (event_id, f"agent:{actor}", prompt_version, now,
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
                (event_id, n["symbol"], f"agent:{actor}", prompt_version,
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
    parser.add_argument("--symbol", default=None,
                        help="export：只导出与该股关联的未评价事件（个股深查）")
    parser.add_argument("--out", default=None, help="export：底稿输出路径")
    parser.add_argument("--tags", default=None, help="import：标签 JSONL 路径")
    parser.add_argument("--actor", default="agent",
                        help="import：打标主体标识（model 记 agent:<actor>）")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        if args.command == "export":
            default_out = f"data/llm_tags/{args.as_of}" + (
                f"_{args.symbol.replace('.', '')}" if args.symbol else "")
            out = Path(args.out or f"{default_out}_input.jsonl")
            n = export_inputs(conn, args.as_of, args.limit, out, symbol=args.symbol)
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
