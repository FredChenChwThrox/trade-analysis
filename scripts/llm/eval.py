"""LLM 评价链编排（消息面 r2 §6.1：6b1 事件级 → 6c 关联 → 6b2 逐股叙事）。

执行顺序解决循环依赖：事件级初判先写 scope，关联层再补 event_symbols，最后逐股叙事。
失败语义（r2 §9）：单条 LLM 失败/JSON 非法 → 丢弃该条不写库（不冒充），聚合计入
stage degraded；整体不抛异常，不阻断报告阶段。
人审 gate（r2 §6.3）：materiality∈{high,critical} 或 confidence<0.4 或 rationale 命中
禁用词 或 (scope=company 且 source_tier≤2) → status='needs_review'，否则 'ok'。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import jsonschema

from scripts.llm import prompts, schema
from scripts.llm.client import LLMClient, LLMConfig, LLMDisabled, LLMError, load_config
from scripts.signals import event_link


@dataclass
class EvalResult:
    status: str = "ok"                     # ok / disabled / degraded
    assessed: int = 0                      # 6b1 事件级行数
    narratives: int = 0                    # 6b2 逐股行数
    skipped: int = 0                       # 丢弃（调用失败/JSON 非法）
    links_added: int = 0
    scope_updated: int = 0
    notes: list[str] = field(default_factory=list)


def gate(cfg: LLMConfig, scope: str, source_tier: int | None, result: dict) -> str:
    """混合人审 gate（r2 §6.3）。"""
    g = cfg.review_gate
    if result["materiality"] in set(g.get("high_materiality") or []):
        return "needs_review"
    if float(result["confidence"]) < float(g.get("low_confidence", 0.4)):
        return "needs_review"
    rationale = result["rationale"]
    if any(word in rationale for word in (g.get("banned_words") or [])):
        return "needs_review"
    if (scope == "company" and source_tier is not None
            and source_tier <= int(g.get("company_tier_max", 2))):
        return "needs_review"
    return "ok"


def _macro_lines(conn: sqlite3.Connection, as_of: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT code, name, close, unit, trade_date, MAX(trade_date)
        FROM macro_factors WHERE trade_date <= ?
        GROUP BY code ORDER BY code
        """, (as_of,)).fetchall()
    return [f"{r['code']} {r['name']} {r['close']}{r['unit'] or ''}（{r['trade_date']}）"
            for r in rows]


def run_llm_eval(conn: sqlite3.Connection, *, run_id: str, as_of: str,
                 client: LLMClient | None = None,
                 cfg: LLMConfig | None = None) -> EvalResult:
    """单次 LLM 评价链。client/cfg 可注入（测试用 fake）。"""
    res = EvalResult()
    cfg = cfg or load_config()
    if not cfg.enabled:
        res.status = "disabled"
        res.notes.append("llm disabled（config/llm.yaml enabled=false，设计关闭非 degraded）")
        return res
    client = client or LLMClient(cfg)
    if not client.available():
        res.status = "disabled"
        res.notes.append(f"llm api key missing（env {cfg.api_key_env}）")
        return res

    # ---- 6b1 事件级初判：未评价过的事件（available_at 点时口径 §2.1） ----
    candidates = conn.execute(
        """
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
        """, (as_of, cfg.max_llm_calls_per_run)).fetchall()
    macro_lines = _macro_lines(conn, as_of)
    for ev in candidates:
        system, user = prompts.event_prompt(
            {"event_type": ev["event_type"], "source_tier": ev["source_tier"],
             "published_at": ev["published_at"], "title": ev["title"],
             "summary": ev["summary"]}, macro_lines)
        try:
            result = client.complete_json(system, user)
            jsonschema.validate(result, schema.EVENT_ASSESSMENT_SCHEMA)
        except (LLMError, LLMDisabled, jsonschema.ValidationError) as exc:
            res.skipped += 1
            res.notes.append(f"6b1 丢弃 {ev['event_id']}: {type(exc).__name__}")
            continue
        scope = result["scope"]
        status = gate(cfg, scope, ev["source_tier"], result)
        conn.execute(
            """
            INSERT OR REPLACE INTO event_assessments (
                event_id, symbol, assessment_version, model, prompt_version,
                assessed_at, event_type, direction, materiality, confidence,
                rationale, target, half_life, expectation_gap, action_hint,
                falsification, status, run_id)
            VALUES (?, '__event__', 'llm_v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ev["event_id"], cfg.model, cfg.prompt_version, run_id,
             ev["event_type"], result["direction"], result["materiality"],
             result["confidence"], result["rationale"], result.get("target"),
             result.get("half_life"), result.get("expectation_gap"),
             result["action_hint"], result.get("falsification_suggestion"),
             status, run_id))
        conn.execute("UPDATE events SET scope = ? WHERE event_id = ?",
                     (scope, ev["event_id"]))
        res.assessed += 1

    # ---- 6c 关联层（确定性） ----
    link_stats = event_link.run_link_stage(conn)
    res.scope_updated = link_stats["scope_updated"]
    res.links_added = link_stats["links_added"]

    # ---- 6b2 逐股叙事：status='ok' 的事件 × 已关联 watchlist 股 ----
    nar_targets = conn.execute(
        """
        SELECT a.event_id, es.symbol, e.title, e.summary, e.published_at
        FROM event_assessments a
        JOIN events e ON e.event_id = a.event_id
        JOIN event_symbols es ON es.event_id = a.event_id
        WHERE a.symbol = '__event__' AND a.assessment_version = 'llm_v1'
          AND a.status = 'ok'
          AND NOT EXISTS (SELECT 1 FROM event_assessments na
                          WHERE na.event_id = a.event_id AND na.symbol = es.symbol
                            AND na.assessment_version = 'llm_v1')
        ORDER BY a.assessed_at DESC
        """).fetchall()
    for row in nar_targets:
        industry = conn.execute(
            "SELECT industry_name FROM symbol_industry WHERE symbol = ? "
            "ORDER BY classification_date DESC LIMIT 1", (row["symbol"],)).fetchone()
        system, user = prompts.narrative_prompt(
            {"title": row["title"], "summary": row["summary"],
             "published_at": row["published_at"]},
            row["symbol"], industry["industry_name"] if industry else None)
        try:
            result = client.complete_json(system, user)
            jsonschema.validate(result, schema.NARRATIVE_SCHEMA)
        except (LLMError, LLMDisabled, jsonschema.ValidationError) as exc:
            res.skipped += 1
            res.notes.append(f"6b2 丢弃 {row['event_id']}/{row['symbol']}: "
                             f"{type(exc).__name__}")
            continue
        ev_status = conn.execute(
            "SELECT status FROM event_assessments WHERE event_id = ? "
            "AND symbol = '__event__' AND assessment_version = 'llm_v1'",
            (row["event_id"],)).fetchone()
        conn.execute(
            """
            INSERT OR REPLACE INTO event_assessments (
                event_id, symbol, assessment_version, model, prompt_version,
                assessed_at, event_type, direction, materiality, confidence,
                rationale, status, narrative, run_id)
            VALUES (?, ?, 'llm_v1', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
            """,
            (row["event_id"], row["symbol"], cfg.model, cfg.prompt_version,
             run_id, ev_status["status"] if ev_status else "ok",
             result["narrative"], run_id))
        res.narratives += 1

    if res.skipped:
        res.status = "degraded"
    return res



def main(argv: list[str] | None = None) -> int:
    """手触发 LLM 评价链（review/补跑用；日常由 daily 步骤 6b 自动执行）。"""
    import argparse
    from dataclasses import replace

    from scripts.pipeline.db import DEFAULT_DB_PATH, connect

    parser = argparse.ArgumentParser(prog="scripts.llm.eval")
    parser.add_argument("--as-of", required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--limit", type=int, default=None,
                        help="临时覆盖 max_llm_calls_per_run（抽检用）")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.limit:
        cfg = replace(cfg, max_llm_calls_per_run=args.limit)
    conn = connect(args.db)
    try:
        res = run_llm_eval(
            conn, run_id=f"llm_eval_{args.as_of}",
            as_of=f"{args.as_of}T23:59:59+00:00", cfg=cfg)
    finally:
        conn.close()
    print(f"[llm_eval] {res.status} assessed={res.assessed} narratives={res.narratives} "
          f"skipped={res.skipped} links=+{res.links_added} scope~{res.scope_updated}")
    for note in res.notes:
        print(f"  {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
