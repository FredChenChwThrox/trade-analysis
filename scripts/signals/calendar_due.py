"""日历到期提醒查询（消息面 r2 Phase 1，L0 日历层消费端）。

两类来源 union 为统一 dict（kind/symbol/date/note）：
1. KIND_CALENDAR——event_calendar 行；窗口按每行 remind_before_days（含两端边界：
   scheduled_date BETWEEN as_of AND date(as_of, '+' || remind_before_days || ' days')）。
   kind 保留表内原始值（report_disclosure/unlock/macro_release/fomc），消费端自行映射标签。
2. KIND_CARD_REVIEW——排期卡复核到期（strategy_card_versions.next_review_at <= as_of
   的 active 卡；派生项不落 event_calendar 表，查询时 union，r2 §3.1）。

纯查询无副作用；scripts/pipeline/report.py（单股过滤：本股 + 宏观 + 本股卡片，
relevant_to_symbol）与 scripts/ui/queries.py（全池横幅）共用本模块。
Phase 4 的 message_judgments 到期弹出将在此追加第三类来源（预留）。
"""

from __future__ import annotations

import sqlite3

KIND_CALENDAR = "calendar"
KIND_CARD_REVIEW = "card_review"


def due_items(conn: sqlite3.Connection, as_of: str) -> list[dict]:
    """as_of 当日（含）起、各 remind_before_days 窗口内的到期项（含排期卡复核逾期）。"""
    items: list[dict] = []
    for r in conn.execute(
        """
        SELECT kind, symbol, scheduled_date, note
        FROM event_calendar
        WHERE scheduled_date >= ?
          AND scheduled_date <= date(?, '+' || remind_before_days || ' days')
        ORDER BY scheduled_date, kind, symbol
        """,
        (as_of, as_of),
    ):
        items.append({"kind": r["kind"], "symbol": r["symbol"],
                      "date": r["scheduled_date"], "note": r["note"]})
    for r in conn.execute(
        """
        SELECT symbol, card_version_id, next_review_at
        FROM strategy_card_versions
        WHERE status = 'active' AND next_review_at IS NOT NULL
          AND next_review_at <= ?
        ORDER BY next_review_at, symbol
        """,
        (as_of,),
    ):
        items.append({"kind": KIND_CARD_REVIEW, "symbol": r["symbol"],
                      "date": r["next_review_at"],
                      "note": f"排期卡复核到期（{r['card_version_id']}）"})
    items.sort(key=lambda it: (it["date"], it["kind"], it["symbol"] or ""))
    return items


def relevant_to_symbol(items: list[dict], symbol: str) -> list[dict]:
    """单股报告过滤：本股日历项 + 宏观项（symbol IS NULL）+ 本股卡片复核；其余股票不出现。"""
    return [
        it for it in items
        if (it["kind"] == KIND_CARD_REVIEW and it["symbol"] == symbol)
        or (it["kind"] != KIND_CARD_REVIEW and it["symbol"] in (symbol, None))
    ]
