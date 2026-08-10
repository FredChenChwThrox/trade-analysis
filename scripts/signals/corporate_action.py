"""公司行为处置（D2.4，设计 §5.4b、§9.1）。

生效卡片存续期间发生除权（凡使 price_adj_factor 变化的事件）时，不复权现价
发生非基本面跳变，卡片触发会产生伪触发。本模块实现三条路径：

1. **检测** `pending_actions`：corporate_actions 中除权日 ≥ 当前 active 卡片
   effective_from、且尚未处理（未被任何版本 input_snapshot_json.conversion
   记录、无未撤销冻结）的事件。
2. **小额现金分红快速通道**：现金分红且除权价变动比例
   （每股分红 ÷ 除权日前一交易日 close_raw）< dividend_fastlane_pct
   （默认 2%，signals.yaml）时，Python 直接按每股分红额减法换算卡片全部
   价格类字段，生成新版本并**自动激活**：新版本 effective_from=ex_date、
   supersedes_id 指向旧版，旧版 status→superseded、effective_to=ex_date
   （排他端点），换算明细入 input_snapshot_json.conversion；写 signal_facts
   行 signal='card_conversion' state='auto_activated'（当日日报据此标注
   "分红自动换算"）。除权日前无行情可算影响比例时**不猜**，降级走三段式。
3. **完整三段式**（送转/拆股/大额分红）：
   - 冻结 `freeze_card`：写 signal_facts signal='suspended_corporate_action'
     state='active'（observed_on=ex_date，details 含事件明细）；自 ex_date 起
     daily_watch/right_side 挂起卡片相关触发（§5.4b 第一步）；
   - 换算 draft `generate_conversion_draft`：机械换算（×1/倍率 或 −每股分红，
     cards.convert_card_fields）生成 status='draft' 新版本（supersedes_id 指向
     旧版，effective_from 留空——未生效），不自动激活；
   - 确认激活由 D2.5 CLI 完成；确认后新版本 active、旧版 effective_to 关闭、
     冻结视为已解（unresolved_suspensions 排除其 ca_id 已入 active 版本
     conversion 的冻结行）。本批另提供 `rescind_suspension`（撤销冻结，
     如事件被证伪）写 signal='suspended_corporate_action_rescinded' 行。

executions 历史记录保留原始成交价与当时 card_version_id，本模块一律不触碰
（§5.4b：不回溯换算，审计沿 supersedes 链与事件倍率重现）。

CLI（检测 + 处置一键跑）：
    uv run python -m scripts.signals.corporate_action <symbol> [--as-of D] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals import cards as card_mod
from scripts.signals.common import RULE_VERSION, load_params

SIGNAL_SUSPENDED = "suspended_corporate_action"
SIGNAL_RESCINDED = "suspended_corporate_action_rescinded"
SIGNAL_CONVERSION = "card_conversion"

SCHEMA_VERSION = "card_v1"


@dataclass
class CAResult:
    symbol: str = ""
    run_id: str = ""
    config_hash: str = ""
    pending: int = 0
    fastlane_activated: list[dict] = field(default_factory=list)
    frozen_drafts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [f"{self.symbol} run_id={self.run_id} 待处理公司行为 {self.pending} 件"]
        for f in self.fastlane_activated:
            lines.append(
                f"  快速通道: ca_id={f['ca_id']} 分红 {f['cash_per_share']}/股"
                f"（影响 {f['impact_pct']:.2%} < 阈值）→ 新版本 {f['new_card_version_id']}"
                f" 自动激活，supersedes {f['supersedes_id']}")
        for f in self.frozen_drafts:
            lines.append(
                f"  三段式: ca_id={f['ca_id']} {f['action_type']} ex_date={f['ex_date']}"
                f" → 已冻结 + 换算 draft {f['draft_card_version_id']}（待 D2.5 人工确认）")
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------- 查询

def unresolved_suspensions(conn: sqlite3.Connection, symbol: str) -> list[dict]:
    """当前未解冻结：state='active' 且未撤销、其 ca_id 未被 active 版本换算吸收。"""
    rows = conn.execute(
        """
        SELECT observed_on, details_json FROM signal_facts
        WHERE symbol = ? AND signal = ? AND state = 'active'
        ORDER BY observed_on
        """,
        (symbol, SIGNAL_SUSPENDED),
    ).fetchall()
    rescinded = {
        int(json.loads(r["details_json"])["ca_id"])
        for r in conn.execute(
            "SELECT details_json FROM signal_facts WHERE symbol = ? AND signal = ?",
            (symbol, SIGNAL_RESCINDED),
        ).fetchall()
    }
    absorbed = set()
    for r in conn.execute(
            "SELECT input_snapshot_json FROM strategy_card_versions "
            "WHERE symbol = ? AND status = 'active'", (symbol,)).fetchall():
        if r["input_snapshot_json"]:
            conv = (json.loads(r["input_snapshot_json"]) or {}).get("conversion") or {}
            if conv.get("ca_id") is not None:
                absorbed.add(int(conv["ca_id"]))
    out = []
    for r in rows:
        det = json.loads(r["details_json"])
        ca_id = int(det["ca_id"])
        if ca_id in rescinded or ca_id in absorbed:
            continue
        out.append({
            "ca_id": ca_id,
            "ex_date": det.get("ex_date", r["observed_on"]),
            "action_type": det.get("action_type"),
        })
    return out


def pending_actions(conn: sqlite3.Connection, symbol: str,
                    as_of: str) -> list[sqlite3.Row]:
    """影响当前 active 卡片且尚未处理的公司行为（除权日 ≥ 卡片生效日，§5.4b）。"""
    card = conn.execute(
        "SELECT * FROM strategy_card_versions WHERE symbol = ? AND status = 'active'",
        (symbol,),
    ).fetchone()
    if card is None or card["effective_from"] is None:
        return []
    handled = card_mod.handled_ca_ids(conn, symbol)
    handled |= {s["ca_id"] for s in unresolved_suspensions(conn, symbol)}
    rows = conn.execute(
        """
        SELECT * FROM corporate_actions
        WHERE symbol = ? AND ex_date >= ? AND ex_date <= ?
        ORDER BY ex_date
        """,
        (symbol, card["effective_from"], as_of),
    ).fetchall()
    return [r for r in rows if int(r["ca_id"]) not in handled]


# ---------------------------------------------------------------- 换算与新版本

def _new_card_id(symbol: str, ca_id: int) -> str:
    return f"{symbol.replace('.', '')}_ca{ca_id}_{uuid.uuid4().hex[:8]}"


def _insert_version(conn: sqlite3.Connection, source: sqlite3.Row, ca: sqlite3.Row,
                    op: str, amount: Decimal, status: str, effective_from: str | None,
                    run_id: str, extra_snapshot: dict | None = None) -> str:
    """按换算结果插入新卡片版本（draft 或 active），返回 card_version_id。"""
    new_id = _new_card_id(source["symbol"], ca["ca_id"])
    converted = card_mod.convert_card_fields(source, op, amount)
    snapshot = card_mod.conversion_snapshot(
        source["card_version_id"], ca, op, amount, extra_snapshot)
    conn.execute(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status,
            schema_version, created_at, effective_from, effective_to, supersedes_id,
            currency, price_basis, earnings_scenarios_json,
            valuation_scenarios_json, price_tiers_json, invalidation_json,
            swing_box_json, right_side_trigger_json, next_review_at,
            input_snapshot_json, run_id)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id, source["symbol"], status, source["schema_version"], utc_now(),
            effective_from, source["card_version_id"], source["currency"],
            source["price_basis"],
            converted.get("earnings_scenarios_json", source["earnings_scenarios_json"]),
            source["valuation_scenarios_json"],  # PE 刻度不动（§5.4b）
            converted.get("price_tiers_json", source["price_tiers_json"]),
            converted.get("invalidation_json", source["invalidation_json"]),
            converted.get("swing_box_json", source["swing_box_json"]),
            converted.get("right_side_trigger_json", source["right_side_trigger_json"]),
            source["next_review_at"],
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            run_id,
        ),
    )
    return new_id


def _prev_close(conn: sqlite3.Connection, symbol: str, ex_date: str) -> Decimal | None:
    r = conn.execute(
        "SELECT close_raw FROM daily_bars WHERE symbol = ? AND trade_date < ? "
        "ORDER BY trade_date DESC LIMIT 1",
        (symbol, ex_date),
    ).fetchone()
    return Decimal(str(r["close_raw"])) if r and r["close_raw"] else None


def fastlane_activate(conn: sqlite3.Connection, source: sqlite3.Row, ca: sqlite3.Row,
                      prev_close: Decimal, run_id: str,
                      config_hash: str) -> dict:
    """快速通道：减法换算 + 自动激活（旧版 superseded，effective_to=ex_date）。"""
    cash = Decimal(str(ca["cash_per_share"]))
    impact = cash / prev_close
    # 先关闭旧版（uq_card_active 部分唯一索引要求任一时刻至多一个 active）
    conn.execute(
        "UPDATE strategy_card_versions SET status = 'superseded', effective_to = ? "
        "WHERE card_version_id = ?",
        (ca["ex_date"], source["card_version_id"]),
    )
    new_id = _insert_version(
        conn, source, ca, "subtract", cash, "active", ca["ex_date"], run_id,
        {"channel": "dividend_fastlane", "prev_close_raw": str(prev_close),
         "impact_pct": float(impact)})
    conn.execute(
        """
        INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
            triggered, active_until, details_json, run_id, rule_version,
            config_hash, created_at)
        VALUES (?, ?, ?, 'auto_activated', NULL, 1, NULL, ?, ?, ?, ?, ?)
        """,
        (source["symbol"], ca["ex_date"], SIGNAL_CONVERSION,
         json.dumps({
             "ca_id": ca["ca_id"], "action_type": ca["action_type"],
             "cash_per_share": str(cash), "prev_close_raw": str(prev_close),
             "impact_pct": float(impact), "channel": "dividend_fastlane",
             "source_card_version_id": source["card_version_id"],
             "new_card_version_id": new_id,
         }, ensure_ascii=False, sort_keys=True),
         run_id, RULE_VERSION, config_hash, utc_now()),
    )
    return {"ca_id": ca["ca_id"], "cash_per_share": str(cash),
            "impact_pct": float(impact), "new_card_version_id": new_id,
            "supersedes_id": source["card_version_id"]}


def freeze_card(conn: sqlite3.Connection, source: sqlite3.Row, ca: sqlite3.Row,
                run_id: str, config_hash: str, note: str | None = None) -> None:
    """三段式第一步：即时冻结（§5.4b）。幂等：同 ca_id 已冻结则跳过。"""
    det = {
        "ca_id": ca["ca_id"], "ex_date": ca["ex_date"],
        "action_type": ca["action_type"],
        "cash_per_share": ca["cash_per_share"], "split_ratio": ca["split_ratio"],
        "card_version_id": source["card_version_id"],
    }
    if note:
        det["note"] = note
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_facts (symbol, observed_on, signal, state,
            anchor_id, triggered, active_until, details_json, run_id, rule_version,
            config_hash, created_at)
        VALUES (?, ?, ?, 'active', NULL, 1, NULL, ?, ?, ?, ?, ?)
        """,
        (source["symbol"], ca["ex_date"], SIGNAL_SUSPENDED,
         json.dumps(det, ensure_ascii=False, sort_keys=True),
         run_id, RULE_VERSION, config_hash, utc_now()),
    )


def generate_conversion_draft(conn: sqlite3.Connection, source: sqlite3.Row,
                              ca: sqlite3.Row, run_id: str,
                              note: str | None = None) -> str:
    """三段式第二步：机械换算生成 draft（不激活，待 D2.5 人工确认）。

    送转/拆股 × 1/倍率；大额分红 − 每股分红。返回 draft card_version_id。
    """
    if ca["split_ratio"] is not None and ca["action_type"] in (
            "split", "bonus_share"):
        ratio = Decimal(str(ca["split_ratio"]))
        op, amount = "multiply", Decimal("1") / ratio
    elif ca["cash_per_share"] is not None:
        op, amount = "subtract", Decimal(str(ca["cash_per_share"]))
    else:
        raise ValueError(
            f"ca_id={ca['ca_id']} 既无 split_ratio 又无 cash_per_share，无法机械换算")
    return _insert_version(conn, source, ca, op, amount, "draft", None, run_id,
                           {"channel": "three_step_draft", "note": note})


def rescind_suspension(conn: sqlite3.Connection, symbol: str, ca_id: int,
                       as_of: str, reason: str, run_id: str,
                       config_hash: str) -> None:
    """撤销冻结（如事件被证伪/撤销）：写 rescinded 行，监测自下一批次恢复。"""
    conn.execute(
        """
        INSERT INTO signal_facts (symbol, observed_on, signal, state, anchor_id,
            triggered, active_until, details_json, run_id, rule_version,
            config_hash, created_at)
        VALUES (?, ?, ?, 'rescinded', NULL, 0, NULL, ?, ?, ?, ?, ?)
        """,
        (symbol, as_of, SIGNAL_RESCINDED,
         json.dumps({"ca_id": ca_id, "reason": reason},
                    ensure_ascii=False, sort_keys=True),
         run_id, RULE_VERSION, config_hash, utc_now()),
    )


# ---------------------------------------------------------------- 检测 + 处置

def process_pending(conn: sqlite3.Connection, symbol: str,
                    as_of: str | None = None, *,
                    run_id: str | None = None,
                    params: dict | None = None,
                    config_hash: str | None = None) -> CAResult:
    """检测并处置待处理公司行为（调用方负责事务/提交；幂等可重跑）。"""
    started_at = utc_now()
    if params is None or config_hash is None:
        params, config_hash = load_params()
    if as_of is None:
        r = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars WHERE symbol = ?", (symbol,)
        ).fetchone()
        as_of = r["d"] if r and r["d"] else datetime.now(timezone.utc).date().isoformat()
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"corporate_action_{symbol}_{now_compact}"
    res = CAResult(symbol=symbol, run_id=run_id, config_hash=config_hash)

    fastlane_pct = float(params["corporate_action"]["dividend_fastlane_pct"])
    pending = pending_actions(conn, symbol, as_of)
    res.pending = len(pending)
    if not pending:
        res.notes.append("无待处理公司行为")
        return res

    for ca in pending:
        # 每个事件针对处置时刻的 active 卡片（fastlane 激活后后续事件基于新卡）
        source = conn.execute(
            "SELECT * FROM strategy_card_versions WHERE symbol = ? AND status = 'active'",
            (symbol,),
        ).fetchone()
        if source is None:
            res.notes.append(f"ca_id={ca['ca_id']}: 无 active 卡片，跳过（§2.5）")
            continue
        if ca["action_type"] == "cash_dividend" and ca["cash_per_share"] is not None:
            prev_close = _prev_close(conn, symbol, ca["ex_date"])
            if prev_close is None:
                note = "除权日前无行情，无法计算影响比例，降级三段式（§2.5 不猜）"
                res.notes.append(f"ca_id={ca['ca_id']}: {note}")
                freeze_card(conn, source, ca, run_id, config_hash, note)
                draft_id = generate_conversion_draft(conn, source, ca, run_id, note)
            else:
                impact = Decimal(str(ca["cash_per_share"])) / prev_close
                if impact < Decimal(str(fastlane_pct)):
                    res.fastlane_activated.append(fastlane_activate(
                        conn, source, ca, prev_close, run_id, config_hash))
                    continue
                note = f"分红影响 {float(impact):.2%} ≥ 阈值 {fastlane_pct:.0%}，走三段式"
                freeze_card(conn, source, ca, run_id, config_hash, note)
                draft_id = generate_conversion_draft(conn, source, ca, run_id, note)
        else:  # 送转/拆股一律三段式
            freeze_card(conn, source, ca, run_id, config_hash)
            draft_id = generate_conversion_draft(conn, source, ca, run_id)
        res.frozen_drafts.append({
            "ca_id": ca["ca_id"], "action_type": ca["action_type"],
            "ex_date": ca["ex_date"], "draft_card_version_id": draft_id,
        })

    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, card_version_id,
            status, error, started_at, finished_at)
        VALUES (?, 'corporate_action', ?, ?, NULL, ?, ?, NULL, 'success', NULL, ?, ?)
        """,
        (run_id, utc_now(), as_of, config_hash, RULE_VERSION, started_at, utc_now()),
    )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.signals.corporate_action")
    parser.add_argument("symbol")
    parser.add_argument("--as-of", default=None, help="数据截止交易日，默认最新 bar")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        with conn:
            res = process_pending(conn, args.symbol, as_of=args.as_of)
        print(res)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
