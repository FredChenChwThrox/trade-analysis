"""执行记录 CLI（D2.5，设计 §5.7、§8.3）。

`executions` append-only：
- `add <symbol>`：记录人工确认的实际动作（系统不因信号触发自动生成成交记录）。
  必须关联当前 active 卡片（card_version_id 从 strategy_card_versions 取，
  无 active 卡拒绝，§2.5/§5.7）；signal_snapshot_json 自动冻结当时相关
  signal_facts 快照（周报五项衰竭信号 + 锚点 + 日频监测 + 右侧状态机），
  冻结后不随信号重算变化（§2.3）。
- 幂等：idempotency_key 全表唯一；未显式提供时按
  (symbol, action_type, price, quantity, tier, fees, executed 日期) 派生
  确定性 key，同参数重试自动去重；重复 key 拒绝（退出码 2）。
- `reverse <execution_id>`：新增冲正记录（action_type='reversal'，
  reverses_execution_id 指向原记录），不改原记录（§5.7 冲正不删改）；
  同一记录只能冲正一次（重复 reverse 拒绝，幂等）。
- 价格/数量/费用为关键决策值，存定点十进制字符串（§9.5）。

CLI：
    uv run python -m scripts.pipeline.execution add 603605.SH \
        --action-type buy --price 57.72 --quantity 100 [--tier 1] [--fees 5.00] \
        [--idempotency-key K] [--executed-at ISO]
    uv run python -m scripts.pipeline.execution reverse <execution_id> [--reason TEXT]
    uv run python -m scripts.pipeline.execution list <symbol>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals import cards as card_mod
from scripts.signals.common import WEEKLY_SIGNALS

ADD_ACTIONS = ("buy", "sell")


class ExecutionCLIError(Exception):
    """可预期的 CLI 错误（退出码 2）。"""


# ---------------------------------------------------------------- 快照冻结（§2.3、§5.7）

def freeze_signal_snapshot(conn: sqlite3.Connection, symbol: str,
                           card_version_id: str, as_of: str) -> dict:
    """冻结执行时点相关 signal_facts 快照（每类信号取 ≤ as_of 最新一行）。"""
    signals = WEEKLY_SIGNALS + [
        "daily_watch", "tier_proximity", "tier_triggered",
        "falsification_breach", "box_position", "ma_comparison",
        "right_side", "suspended_corporate_action",
    ]
    facts: dict[str, dict] = {}
    for sig in signals:
        r = conn.execute(
            """
            SELECT observed_on, state, triggered, active_until, anchor_id,
                   details_json
            FROM signal_facts
            WHERE symbol = ? AND signal = ? AND observed_on <= ?
            ORDER BY observed_on DESC LIMIT 1
            """,
            (symbol, sig, as_of),
        ).fetchone()
        if r is not None:
            facts[sig] = {
                "observed_on": r["observed_on"], "state": r["state"],
                "triggered": r["triggered"], "active_until": r["active_until"],
                "anchor_id": r["anchor_id"],
                "details": json.loads(r["details_json"]) if r["details_json"] else None,
            }
    anchors = [
        dict(row) for row in conn.execute(
            """
            SELECT anchor_id, anchor_type, trade_date, adjusted_price, raw_price,
                   is_fallback, as_of
            FROM weekly_anchors
            WHERE symbol = ? AND as_of = (
                SELECT MAX(as_of) FROM weekly_anchors WHERE symbol = ? AND as_of <= ?)
            """,
            (symbol, symbol, as_of),
        )
    ]
    return {
        "symbol": symbol, "as_of": as_of, "card_version_id": card_version_id,
        "frozen_at": utc_now(),
        "signal_facts": facts, "weekly_anchors": anchors,
    }


# ---------------------------------------------------------------- add

def _dec_str(value: str, field: str) -> str:
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise ExecutionCLIError(f"{field} 不是合法十进制数: {value!r}")
    return format(d, "f")  # 保留输入精度（5.00 存 "5.00"），禁用科学计数法


def derive_idempotency_key(symbol: str, action_type: str, price: str,
                           quantity: str, tier: str | None, fees: str | None,
                           executed_day: str) -> str:
    """未显式提供 key 时的确定性派生（同参数重试自动去重）。"""
    raw = "|".join([symbol, action_type, price, quantity,
                    tier or "", fees or "", executed_day])
    return "auto_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def add_execution(conn: sqlite3.Connection, symbol: str, *,
                  action_type: str, price: str, quantity: str,
                  tier: str | None = None, fees: str | None = None,
                  idempotency_key: str | None = None,
                  executed_at: str | None = None,
                  backfill: bool = False, note: str | None = None) -> dict:
    """插入执行记录（调用方不需要外层事务，本函数自管）。

    backfill=True 用于补录系统上线前的手工执行：关联"当前"active 卡片
    （而非 executed_day 当时的卡片，彼时系统尚不存在），snapshot 只记
    backfill 标记与说明，不冻结信号事实（§5.7 审计语义：如实声明补录）。
    """
    if action_type not in ADD_ACTIONS:
        raise ExecutionCLIError(
            f"action_type 只接受 {ADD_ACTIONS}（冲正请用 reverse 命令）")
    price_s, quantity_s = _dec_str(price, "price"), _dec_str(quantity, "quantity")
    fees_s = _dec_str(fees, "fees") if fees is not None else None
    if executed_at is None:
        executed_at = utc_now()
    executed_day = executed_at[:10]

    if backfill:
        card = card_mod.load_active_card(conn, symbol, utc_now()[:10])
        if card is None:
            raise ExecutionCLIError(
                f"{symbol} 当前无 active 卡片，补录无法关联卡片（先激活卡片再补录）")
    else:
        # 必须关联当前 active 卡片（§5.7；无卡拒绝，不猜）
        card = card_mod.load_active_card(conn, symbol, executed_day)
        if card is None:
            raise ExecutionCLIError(
                f"{symbol} 在 {executed_day} 无 active 卡片，拒绝记录执行"
                "（§5.7：执行必须关联当前 active 卡片）")
    if idempotency_key is None:
        idempotency_key = derive_idempotency_key(
            symbol, action_type, price_s, quantity_s, tier, fees_s, executed_day)
    dup = conn.execute(
        "SELECT execution_id FROM executions WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if dup is not None:
        raise ExecutionCLIError(
            f"idempotency_key 重复（已存在 execution_id={dup['execution_id']}），"
            "拒绝重复记录（§8.3）")

    if backfill:
        as_of = executed_day
        snapshot = {
            "backfill": True,
            "note": note or "系统上线前手工执行，补录",
            "registered_under_card": card.card_version_id,
            "registered_at": utc_now(),
        }
    else:
        r = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars "
            "WHERE symbol = ? AND trade_date <= ?",
            (symbol, executed_day),
        ).fetchone()
        as_of = r["d"] or executed_day
        snapshot = freeze_signal_snapshot(conn, symbol, card.card_version_id, as_of)

    with conn:
        cur = conn.execute(
            """
            INSERT INTO executions (idempotency_key, symbol, executed_at,
                action_type, tier, price, quantity, fees, card_version_id,
                signal_snapshot_json, reverses_execution_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (idempotency_key, symbol, executed_at, action_type, tier,
             price_s, quantity_s, fees_s, card.card_version_id,
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True), utc_now()),
        )
    return {"execution_id": cur.lastrowid, "idempotency_key": idempotency_key,
            "card_version_id": card.card_version_id, "snapshot_as_of": as_of}


# ---------------------------------------------------------------- reverse

def reverse_execution(conn: sqlite3.Connection, execution_id: int,
                      reason: str | None = None) -> dict:
    """新增冲正记录（不修改原记录，§5.7）。同一记录只能冲正一次。"""
    orig = conn.execute(
        "SELECT * FROM executions WHERE execution_id = ?", (execution_id,),
    ).fetchone()
    if orig is None:
        raise ExecutionCLIError(f"execution_id={execution_id} 不存在")
    existing = conn.execute(
        "SELECT execution_id FROM executions WHERE reverses_execution_id = ?",
        (execution_id,),
    ).fetchone()
    if existing is not None:
        raise ExecutionCLIError(
            f"execution_id={execution_id} 已被冲正"
            f"（reversal execution_id={existing['execution_id']}），拒绝重复冲正")
    key = f"rev_{execution_id}_{orig['idempotency_key']}"
    snapshot = {
        "reversal_of": execution_id,
        "original_action_type": orig["action_type"],
        "original_executed_at": orig["executed_at"],
        "original_card_version_id": orig["card_version_id"],
        "reason": reason,
        "frozen_at": utc_now(),
    }
    with conn:
        cur = conn.execute(
            """
            INSERT INTO executions (idempotency_key, symbol, executed_at,
                action_type, tier, price, quantity, fees, card_version_id,
                signal_snapshot_json, reverses_execution_id, created_at)
            VALUES (?, ?, ?, 'reversal', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, orig["symbol"], utc_now(), orig["tier"], orig["price"],
             orig["quantity"], orig["fees"], orig["card_version_id"],
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
             execution_id, utc_now()),
        )
    return {"execution_id": cur.lastrowid, "reverses": execution_id,
            "idempotency_key": key}


# ---------------------------------------------------------------- list

def cmd_list(conn: sqlite3.Connection, symbol: str) -> None:
    rows = conn.execute(
        "SELECT * FROM executions WHERE symbol = ? ORDER BY execution_id",
        (symbol,),
    ).fetchall()
    reversed_by = {r["reverses_execution_id"]: r["execution_id"] for r in rows
                   if r["reverses_execution_id"] is not None}
    print(f"{symbol} 执行记录 {len(rows)} 条（append-only，§5.7）：")
    if not rows:
        print("  （无）")
    for r in rows:
        mark = ""
        if r["execution_id"] in reversed_by:
            mark = f"  [已被 #{reversed_by[r['execution_id']]} 冲正]"
        if r["action_type"] == "reversal":
            mark = f"  [冲正 #{r['reverses_execution_id']}]"
        print(f"  #{r['execution_id']} {r['executed_at']} {r['action_type']} "
              f"tier={r['tier'] or '—'} price={r['price']} qty={r['quantity']} "
              f"fees={r['fees'] or '—'} card={r['card_version_id']} "
              f"key={r['idempotency_key']}{mark}")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.execution")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="记录人工确认的执行（需 active 卡片）")
    p.add_argument("symbol")
    p.add_argument("--action-type", required=True, choices=ADD_ACTIONS)
    p.add_argument("--price", required=True)
    p.add_argument("--quantity", required=True)
    p.add_argument("--tier", default=None)
    p.add_argument("--fees", default=None)
    p.add_argument("--idempotency-key", default=None)
    p.add_argument("--executed-at", default=None, help="ISO 时间，默认当前 UTC")
    p.add_argument("--backfill", action="store_true",
                   help="补录系统上线前的手工执行（关联当前 active 卡，snapshot 记 backfill 标记）")
    p.add_argument("--note", default=None, help="备注（仅 --backfill 时写入 snapshot）")

    p = sub.add_parser("reverse", help="冲正已有记录（新增 reversal，不改原记录）")
    p.add_argument("execution_id", type=int)
    p.add_argument("--reason", default=None)

    p = sub.add_parser("list", help="列出该股执行记录与冲正链")
    p.add_argument("symbol")

    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        if args.cmd == "add":
            res = add_execution(
                conn, args.symbol, action_type=args.action_type, price=args.price,
                quantity=args.quantity, tier=args.tier, fees=args.fees,
                idempotency_key=args.idempotency_key,
                executed_at=args.executed_at,
                backfill=args.backfill, note=args.note)
            print(f"执行已记录 #{res['execution_id']} "
                  f"（card={res['card_version_id']}，快照截止 {res['snapshot_as_of']}，"
                  f"key={res['idempotency_key']}）")
        elif args.cmd == "reverse":
            res = reverse_execution(conn, args.execution_id, args.reason)
            print(f"冲正已记录 #{res['execution_id']}（冲正 #{res['reverses']}，"
                  f"key={res['idempotency_key']}）")
        elif args.cmd == "list":
            cmd_list(conn, args.symbol)
        return 0
    except ExecutionCLIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
