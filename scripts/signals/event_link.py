"""事件分发/关联层（消息面 r2 §5，L2 确定性——无 LLM）。

职责：
1. scope 关键词初分（macro/policy；announcement 天然 company；flow 天然 flow），
   留空者由 LLM 6b1 复核修正（r2 §5.2）；
2. 关联候选（r2 §5.3）：① event_symbols 已含（保留，INSERT OR IGNORE 不破坏手工关联）；
   ② symbol_industry 行业名匹配（scope∈industry/policy/macro）；③ watchlist themes_json
   词边界匹配（避免"黄金"命中"黄金周"）；
3. 路由规则：macro/policy → 周度复核队列（不推送）；company tier≤2 → 报告置顶+
   "需读原文"；flow → 静默（r2 §8.4 已在入库端保证）。

effective_status 解析（r2 §3.3，人审不改写原始行）：
未撤销 dismiss → 排除；upgrade_materiality → 覆盖 materiality 显示；confirm → ok；
amend → payload 覆盖 expectation_gap/falsification/target/half_life 显示值；
否则取 event_assessments.status。事件级（'__event__'）先应用，逐股后应用（specific 覆盖 general）。
"""

from __future__ import annotations

import json
import re
import sqlite3

MACRO_KEYWORDS = ("降准", "降息", "加息", "MLF", "LPR", "逆回购", "存款准备金",
                  "美联储", "FOMC", "CPI", "PPI", "社融", "汇率", "国债")
POLICY_KEYWORDS = ("发改委", "财政部", "央行", "证监会", "国资委", "工信部",
                   "商务部", "住建部", "国务院", "集采", "征税", "关税")
_AMEND_FIELDS = ("expectation_gap", "falsification", "target", "half_life")

def classify_scope(event_type: str, title: str, summary: str | None) -> str | None:
    """关键词初分 scope；不可判定返回 None（LLM 6b1 复核修正）。

    顺序：flow/company 天然判定 → macro 关键词（r2 例：降准/MLF→macro，
    "央行降准"归 macro 不归 policy）→ policy 部委名 → None。
    """
    if event_type == "flow":
        return "flow"
    if event_type == "announcement":
        return "company"
    text = f"{title} {summary or ''}"
    if any(kw in text for kw in MACRO_KEYWORDS):
        return "macro"
    if any(kw in text for kw in POLICY_KEYWORDS):
        return "policy"
    return None


def _term_hit(term: str, text: str) -> bool:
    """主题词命中：排除已知复合词后缀（"黄金周"≠"黄金"，r2 §5.3 例）。

    口径：只做后向排除（curated 后缀"周/节"——节日复合词），前向不设挡
    （"电解铜"→"铜" 应命中）。precision 局限在 Phase 3 由 LLM 关联+人工确认补。
    """
    if not term:
        return False
    pattern = re.compile(re.escape(term) + r"(?![周节])")
    return bool(pattern.search(text))


def link_event(conn: sqlite3.Connection, event_id: str, title: str,
               summary: str | None, scope: str | None) -> int:
    """按候选规则补 event_symbols（INSERT OR IGNORE），返回新增关联数。"""
    text = f"{title} {summary or ''}"
    added = 0
    if scope in ("industry", "policy", "macro"):
        # ③ watchlist themes_json 词边界
        for sym, themes_json in conn.execute(
                "SELECT symbol, themes_json FROM watchlist WHERE active = 1"):
            try:
                themes = json.loads(themes_json or "[]")
            except json.JSONDecodeError:
                continue
            if any(_term_hit(str(t), text) for t in themes if t):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO event_symbols (event_id, symbol) "
                    "VALUES (?, ?)", (event_id, sym))
                added += cur.rowcount
        # ② symbol_industry 行业名匹配（事件文本命中行业名 → 该行业 watchlist 股）
        for name_row in conn.execute(
                "SELECT DISTINCT industry_name FROM symbol_industry"):
            name = name_row["industry_name"]
            if not _term_hit(name, text):
                continue
            for sym, in conn.execute(
                    "SELECT DISTINCT si.symbol FROM symbol_industry si "
                    "JOIN watchlist w ON w.symbol = si.symbol AND w.active = 1 "
                    "WHERE si.industry_name = ?", (name,)):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO event_symbols (event_id, symbol) "
                    "VALUES (?, ?)", (event_id, sym))
                added += cur.rowcount
    return added


def run_link_stage(conn: sqlite3.Connection) -> dict:
    """对全部 announcement/news 事件补 scope（留空者）并跑关联。池级调用。"""
    scope_updated = 0
    links_added = 0
    rows = conn.execute(
        "SELECT event_id, event_type, title, summary, scope FROM events "
        "WHERE event_type IN ('announcement', 'news')").fetchall()
    for r in rows:
        scope = r["scope"] or classify_scope(r["event_type"], r["title"], r["summary"])
        if scope and scope != r["scope"]:
            conn.execute("UPDATE events SET scope = ? WHERE event_id = ?",
                         (scope, r["event_id"]))
            scope_updated += 1
        links_added += link_event(conn, r["event_id"], r["title"], r["summary"], scope)
    return {"scope_updated": scope_updated, "links_added": links_added}


def resolve_effective(conn: sqlite3.Connection, event_id: str) -> dict:
    """解析事件的有效显示状态（人审 replay）。

    返回 {"hidden": bool, "status": str|None, "direction"/"materiality"/... 显示值,
          "symbols": {sym: 同结构（含 narrative）}}。
    """
    ev = conn.execute(
        "SELECT * FROM event_assessments WHERE event_id = ? AND symbol = '__event__' "
        "AND assessment_version = 'llm_v1'", (event_id,)).fetchone()
    base = {
        "hidden": False, "status": None, "direction": None, "materiality": None,
        "confidence": None, "rationale": None, "target": None, "half_life": None,
        "expectation_gap": None, "falsification": None, "action_hint": None,
        "narrative": None,
    }
    if ev is not None:
        base.update({k: ev[k] for k in
                     ("status", "direction", "materiality", "confidence",
                      "rationale", "target", "half_life", "expectation_gap",
                      "action_hint")})

    def apply_reviews(view: dict, symbol: str) -> None:
        reviews = conn.execute(
            "SELECT action, payload_json FROM event_human_review "
            "WHERE event_id = ? AND symbol = ? ORDER BY reviewed_at",
            (event_id, symbol)).fetchall()
        for r in reviews:
            action, payload = r["action"], {}
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if action == "dismiss":
                view["hidden"] = True
            elif action == "confirm":
                view["hidden"] = False
                view["status"] = "ok"
            elif action == "upgrade_materiality":
                view["materiality"] = payload.get("materiality") or view["materiality"]
            elif action == "amend":
                for f in _AMEND_FIELDS:
                    if payload.get(f) is not None:
                        view[f] = payload[f]
            # note：仅留痕，不改显示

    apply_reviews(base, "__event__")

    symbols: dict[str, dict] = {}
    for row in conn.execute(
            "SELECT DISTINCT symbol FROM event_symbols WHERE event_id = ?",
            (event_id,)):
        sym = row["symbol"]
        nar = conn.execute(
            "SELECT narrative, status FROM event_assessments WHERE event_id = ? "
            "AND symbol = ? AND assessment_version = 'llm_v1'",
            (event_id, sym)).fetchone()
        view = dict(base)
        view["narrative"] = nar["narrative"] if nar else None
        apply_reviews(view, sym)
        symbols[sym] = view
    base["symbols"] = symbols
    return base
