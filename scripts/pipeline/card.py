"""排期卡版本管理 CLI（D2.5，设计 §5.6、§2.4、§5.4b 第三步）。

状态机：draft → active / rejected；active → superseded（激活新版时关闭）。
同一股票同一时刻最多一个 active（uq_card_active 部分唯一索引硬约束）。

- `create-draft <symbol> --json PATH`：校验卡片 JSON（jsonschema + 语义校验）
  后插入 status='draft' 版本；draft 从未生效（effective_from 留空）。
- `activate <card_version_id> [--effective-from D]`：人工确认激活——同一事务内
  关闭旧 active（status→superseded，effective_to=新版 effective_from，排他端点，
  语义见 scripts/signals/cards.py），新版置 active。公司行为换算 draft 也走这里
  确认（§5.4b 第三步）：effective_from 缺省取 input_snapshot_json.conversion
  的 ex_date；确认后 conversion.ca_id 被 active 版本吸收，冻结自动视为已解
  （corporate_action.unresolved_suspensions 口径），监测下一批次恢复。
- `reject <card_version_id>`：draft→rejected；对 active 版本执行 reject 视为
  人工废止（关闭 effective_to=当日），均不改历史 JSON 字段（§5.6 不可变版本）。
- `list <symbol>` / `show <card_version_id>`：只读查询。

激活时渲染 Markdown 到 `cards/{symbol}/{effective_from}_{card_version_id}.md`，
并刷新 `cards/{symbol}/current.md`（当前 active 视图）。Markdown 一律由库记录
渲染，不手工回写数据库（§2.4 唯一事实源）。

CLI：
    uv run python -m scripts.pipeline.card create-draft 603605.SH --json card.json
    uv run python -m scripts.pipeline.card activate <card_version_id> [--effective-from D]
    uv run python -m scripts.pipeline.card reject <card_version_id> [--reason TEXT]
    uv run python -m scripts.pipeline.card list <symbol>
    uv run python -m scripts.pipeline.card show <card_version_id>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import date as date_type
from decimal import Decimal
from pathlib import Path

import jsonschema

from scripts.pipeline.db import DEFAULT_DB_PATH, ROOT, connect, utc_now

SCHEMA_VERSION = "card_v1"
CARDS_ROOT = ROOT / "cards"

_DEC = r"^-?\d+(\.\d{1,4})?$"  # 关键决策值：定点十进制字符串（§9.5）

CARD_INPUT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "strategy_card_input_v1",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "currency": {"type": "string", "minLength": 1},
        "price_basis": {"type": "string", "minLength": 1},
        "next_review_at": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "supersedes_id": {"type": "string", "minLength": 1},
        "earnings": {
            "type": "object",
            "properties": {
                "eps": {
                    "type": "object",
                    "properties": {k: {"type": "string", "pattern": _DEC}
                                   for k in ("bear", "base", "bull")},
                    "required": ["bear", "base", "bull"],
                    "additionalProperties": False,
                },
            },
            "required": ["eps"],
        },
        "valuation": {"type": "object"},  # PE 刻度/情景，须含 sample_window 标注（§3.2）
        "price_tiers": {
            "type": "object",
            "properties": {
                "tiers": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tier": {"type": "integer", "minimum": 1, "maximum": 3},
                            "zone_low": {"type": "string", "pattern": _DEC},
                            "zone_high": {"type": "string", "pattern": _DEC},
                        },
                        "required": ["tier", "zone_low", "zone_high"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tiers"],
            "additionalProperties": False,
        },
        "invalidation": {
            "type": "object",
            "properties": {
                "line": {"type": "string", "pattern": _DEC},
                "note": {"type": "string"},
            },
            "required": ["line"],
        },
        "swing_box": {
            "type": "object",
            "properties": {k: {"type": "string", "pattern": _DEC} for k in (
                "box_low", "box_high", "buy_zone_low", "buy_zone_high",
                "sell_zone_low", "sell_zone_high", "box_invalidation")},
            "additionalProperties": False,
        },
        "right_side_trigger": {
            "type": "object",
            "properties": {
                "trigger_level": {"type": "string", "pattern": _DEC},
                "stop_level": {"type": "string", "pattern": _DEC},
            },
            "additionalProperties": False,
        },
        "input_snapshot": {"type": "object"},
    },
}


class CardCLIError(Exception):
    """可预期的 CLI 错误（退出码 2）。"""


# ---------------------------------------------------------------- 校验

def validate_card_input(doc: dict) -> list[str]:
    """jsonschema 结构校验 + 语义校验（区间方向、重复档位）。返回错误列表。"""
    errors: list[str] = []
    try:
        jsonschema.validate(doc, CARD_INPUT_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
        return [f"schema: {path}: {exc.message}"]

    tiers = (doc.get("price_tiers") or {}).get("tiers") or []
    seen: set[int] = set()
    for t in tiers:
        lo, hi = Decimal(t["zone_low"]), Decimal(t["zone_high"])
        if lo > hi:
            errors.append(f"price_tiers: tier {t['tier']} zone_low > zone_high")
        if t["tier"] in seen:
            errors.append(f"price_tiers: tier {t['tier']} 重复")
        seen.add(t["tier"])
    box = doc.get("swing_box") or {}
    for lo_k, hi_k in (("box_low", "box_high"), ("buy_zone_low", "buy_zone_high"),
                       ("sell_zone_low", "sell_zone_high")):
        if box.get(lo_k) is not None and box.get(hi_k) is not None:
            if Decimal(box[lo_k]) > Decimal(box[hi_k]):
                errors.append(f"swing_box: {lo_k} > {hi_k}")
    return errors


# ---------------------------------------------------------------- 查询

def get_version(conn: sqlite3.Connection, card_version_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM strategy_card_versions WHERE card_version_id = ?",
        (card_version_id,),
    ).fetchone()
    if row is None:
        raise CardCLIError(f"卡片版本不存在: {card_version_id}")
    return row


def _active_version(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM strategy_card_versions WHERE symbol = ? AND status = 'active'",
        (symbol,),
    ).fetchone()


# ---------------------------------------------------------------- Markdown 渲染（§5.6、§2.4）

def _j(text: str | None) -> dict:
    return json.loads(text) if text else {}


def render_card_markdown(row: sqlite3.Row) -> str:
    """由库记录渲染卡片 Markdown（唯一事实源=数据库，§2.4）。"""
    L: list[str] = []
    a = L.append
    a(f"# 排期卡 {row['symbol']} — {row['card_version_id']}")
    a("")
    a("> 本文件由 `strategy_card_versions` 库记录渲染，仅作存档视图，"
      "不手工回写数据库（§2.4）。")
    a("")
    a("## 版本信息")
    a("")
    a(f"- symbol: {row['symbol']}")
    a(f"- card_version_id: `{row['card_version_id']}`")
    a(f"- status: {row['status']}（schema_version={row['schema_version']}）")
    a(f"- created_at: {row['created_at']}")
    a(f"- 生效区间: [{row['effective_from'] or '—'}, {row['effective_to'] or '开口'})"
      "（排他端点）")
    a(f"- supersedes_id: {row['supersedes_id'] or '—'}")
    a(f"- currency: {row['currency'] or '—'} / price_basis: {row['price_basis'] or '—'}"
      "（价区为不复权绝对价位）")
    a(f"- next_review_at: {row['next_review_at'] or '—'}（到期生成复核提醒，不自动延后）")
    a(f"- run_id: {row['run_id'] or '—'}")
    a("")

    earn = _j(row["earnings_scenarios_json"])
    if earn:
        a("## 盈利情景（EPS）")
        a("")
        for k in ("bear", "base", "bull"):
            v = (earn.get("eps") or {}).get(k)
            if v is not None:
                a(f"- {k}: {v}")
        a("")
    val = _j(row["valuation_scenarios_json"])
    if val:
        a("## 估值情景（PE 刻度）")
        a("")
        pe = val.get("pe") or {}
        if pe:
            a("- " + " / ".join(f"{k}: {v}" for k, v in pe.items()))
        if val.get("sample_window"):
            a(f"- 刻度样本区间: {val['sample_window']}（§3.2：3 年样本强制标注）")
        a("")
    tiers = _j(row["price_tiers_json"]).get("tiers") or []
    if tiers:
        a("## 三档价区（不复权）")
        a("")
        a("| 档 | 下沿 | 上沿 | 触发附加条件 |")
        a("|---|---|---|---|")
        for t in sorted(tiers, key=lambda x: x["tier"]):
            extra = "无" if t["tier"] == 1 else "同一锚点活跃衰竭信号 ≥ 2 项"
            a(f"| T{t['tier']} | {t['zone_low']} | {t['zone_high']} | {extra} |")
        a("")
    inv = _j(row["invalidation_json"])
    if inv:
        a("## 证伪线")
        a("")
        a(f"- line: {inv.get('line')}"
          + (f"（{inv['note']}）" if inv.get("note") else ""))
        a("- 有效跌破口径: 收盘 ≤ 线 ×(1−1%) 连续 2 个交易日（config/signals.yaml）")
        a("")
    box = _j(row["swing_box_json"])
    if box:
        a("## 波段箱体（只监测存档边界）")
        a("")
        labels = {"box_low": "箱体下沿", "box_high": "箱体上沿",
                  "buy_zone_low": "买区下沿", "buy_zone_high": "买区上沿",
                  "sell_zone_low": "卖区下沿", "sell_zone_high": "卖区上沿",
                  "box_invalidation": "箱体证伪"}
        for k, label in labels.items():
            if box.get(k) is not None:
                a(f"- {label}: {box[k]}")
        a("")
    rst = _j(row["right_side_trigger_json"])
    if rst:
        a("## 右侧确认")
        a("")
        a(f"- 触发位: {rst.get('trigger_level') or '—'} / 止损位: {rst.get('stop_level') or '—'}")
        a("- 状态机: 收盘突破触发位 1% 且量 ≥ 前 20 日均量 2 倍 → 等待回踩；"
          "10 个交易日内回踩 ±2% 且收盘守住 −1% → confirmed（config/signals.yaml）")
        a("")
    snap = _j(row["input_snapshot_json"])
    if snap:
        a("## 输入快照")
        a("")
        if snap.get("demo"):
            a("- ⚠ demo: true（演示卡，非正式研判产物）")
        conv = snap.get("conversion")
        if conv:
            a(f"- 公司行为换算: ca_id={conv.get('ca_id')} "
              f"{conv.get('action_type')} ex_date={conv.get('ex_date')} "
              f"op={conv.get('op')} factor={conv.get('factor') or '—'} "
              f"cash_per_share={conv.get('cash_per_share') or '—'} "
              f"来源版本 {conv.get('source_card_version_id')}")
        a("")
        a("```json")
        a(json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True))
        a("```")
        a("")
    return "\n".join(L)


def refresh_current_view(conn: sqlite3.Connection, symbol: str,
                         cards_root: Path = CARDS_ROOT) -> Path | None:
    """刷新 current.md 为当前 active 视图；无 active 时删除（§2.4、§5.6）。"""
    d = cards_root / symbol
    current_path = d / "current.md"
    active = _active_version(conn, symbol)
    if active is None:
        if current_path.exists():
            current_path.unlink()
        return None
    d.mkdir(parents=True, exist_ok=True)
    header = (f"<!-- 当前 active 视图，自动刷新自 "
              f"{active['effective_from']}_{active['card_version_id']}.md；"
              f"勿手工编辑（§2.4） -->\n\n")
    current_path.write_text(header + render_card_markdown(active),
                            encoding="utf-8")
    return current_path


def write_card_markdown(conn: sqlite3.Connection, row: sqlite3.Row,
                        cards_root: Path = CARDS_ROOT) -> tuple[Path, Path | None]:
    """写版本文件并刷新 current.md（current = 当前 active 视图）。"""
    d = cards_root / row["symbol"]
    d.mkdir(parents=True, exist_ok=True)
    version_path = d / f"{row['effective_from']}_{row['card_version_id']}.md"
    version_path.write_text(render_card_markdown(row), encoding="utf-8")
    current_path = refresh_current_view(conn, row["symbol"], cards_root)
    return version_path, current_path


# ---------------------------------------------------------------- 命令实现

def create_draft(conn: sqlite3.Connection, symbol: str, json_path: str,
                 *, run_id: str | None = None) -> str:
    """校验并插入 draft，返回 card_version_id。"""
    doc = json.loads(Path(json_path).read_text(encoding="utf-8"))
    errors = validate_card_input(doc)
    if errors:
        raise CardCLIError("卡片 JSON 校验失败:\n  " + "\n  ".join(errors))
    wl = conn.execute(
        "SELECT symbol FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    if wl is None:
        raise CardCLIError(f"{symbol} 不在 watchlist")
    if doc.get("supersedes_id"):
        get_version(conn, doc["supersedes_id"])  # 存在性校验

    card_id = f"{symbol.replace('.', '')}_{uuid.uuid4().hex[:8]}"
    run_id = run_id or f"card_draft_{card_id}"
    snapshot = doc.get("input_snapshot")

    def dump(key: str) -> str | None:
        return (json.dumps(doc[key], ensure_ascii=False, sort_keys=True)
                if doc.get(key) is not None else None)

    with conn:
        conn.execute(
            """
            INSERT INTO strategy_card_versions (card_version_id, symbol, status,
                schema_version, created_at, effective_from, effective_to,
                supersedes_id, currency, price_basis, earnings_scenarios_json,
                valuation_scenarios_json, price_tiers_json, invalidation_json,
                swing_box_json, right_side_trigger_json, next_review_at,
                input_snapshot_json, run_id)
            VALUES (?, ?, 'draft', ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (card_id, symbol, SCHEMA_VERSION, utc_now(), doc.get("supersedes_id"),
             doc.get("currency"), doc.get("price_basis"),
             dump("earnings"), dump("valuation"), dump("price_tiers"),
             dump("invalidation"), dump("swing_box"), dump("right_side_trigger"),
             doc.get("next_review_at"),
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
             if snapshot is not None else None,
             run_id),
        )
    return card_id


def activate(conn: sqlite3.Connection, card_version_id: str,
             effective_from: str | None = None, *,
             run_id: str | None = None,
             cards_root: Path = CARDS_ROOT) -> dict:
    """人工确认激活（§5.6、§5.4b 第三步）。同事务关闭旧 active。"""
    row = get_version(conn, card_version_id)
    if row["status"] == "active":
        raise CardCLIError(f"{card_version_id} 已是 active，无需重复激活")
    if row["status"] != "draft":
        raise CardCLIError(
            f"只有 draft 可激活（当前 status={row['status']}，历史版本不可复活）")
    if effective_from is None:
        conv = (_j(row["input_snapshot_json"]).get("conversion") or {})
        effective_from = conv.get("ex_date")  # 换算 draft 默认除权日生效（§5.4b）
    if effective_from is None:
        effective_from = date_type.today().isoformat()
    date_type.fromisoformat(effective_from)

    old = _active_version(conn, row["symbol"])
    if old is not None and effective_from < (old["effective_from"] or ""):
        raise CardCLIError(
            f"effective_from={effective_from} 早于当前 active 卡生效日 "
            f"{old['effective_from']}，不允许回填历史生效区间（§5.1）")
    run_id = run_id or f"card_activate_{card_version_id}"
    with conn:
        if old is not None:
            conn.execute(
                "UPDATE strategy_card_versions SET status = 'superseded', "
                "effective_to = ? WHERE card_version_id = ?",
                (effective_from, old["card_version_id"]),
            )
        conn.execute(
            "UPDATE strategy_card_versions SET status = 'active', "
            "effective_from = ?, run_id = ? WHERE card_version_id = ?",
            (effective_from, run_id, card_version_id),
        )
        new_row = get_version(conn, card_version_id)
        version_path, current_path = write_card_markdown(conn, new_row, cards_root)
    return {
        "card_version_id": card_version_id, "effective_from": effective_from,
        "superseded": (old["card_version_id"] if old else None),
        "version_path": str(version_path), "current_path": str(current_path),
    }


def reject(conn: sqlite3.Connection, card_version_id: str,
           reason: str | None = None, *, run_id: str | None = None,
           cards_root: Path = CARDS_ROOT) -> dict:
    """拒绝 draft；对 active 执行为人工废止（关 effective_to，不改历史字段）。

    废止 active 后刷新 current.md（无 active 则删除视图，§2.4/§5.6）。
    """
    row = get_version(conn, card_version_id)
    if row["status"] not in ("draft", "active"):
        raise CardCLIError(
            f"只有 draft/active 可拒绝（当前 status={row['status']}）")
    run_id = run_id or f"card_reject_{card_version_id}"
    with conn:
        if row["status"] == "draft":
            conn.execute(
                "UPDATE strategy_card_versions SET status = 'rejected', run_id = ? "
                "WHERE card_version_id = ?",
                (run_id, card_version_id),
            )
        else:  # active → 人工废止：关闭生效区间，历史 JSON 不动
            conn.execute(
                "UPDATE strategy_card_versions SET status = 'rejected', "
                "effective_to = COALESCE(effective_to, ?), run_id = ? "
                "WHERE card_version_id = ?",
                (date_type.today().isoformat(), run_id, card_version_id),
            )
        refresh_current_view(conn, row["symbol"], cards_root)
    return {"card_version_id": card_version_id, "was": row["status"],
            "reason": reason}


# ---------------------------------------------------------------- 展示

def _fmt_row(r: sqlite3.Row) -> str:
    return (f"{r['card_version_id']}  status={r['status']}  "
            f"生效=[{r['effective_from'] or '—'}, {r['effective_to'] or '开口'})  "
            f"supersedes={r['supersedes_id'] or '—'}  "
            f"next_review={r['next_review_at'] or '—'}  created={r['created_at']}")


def cmd_list(conn: sqlite3.Connection, symbol: str) -> None:
    rows = conn.execute(
        "SELECT * FROM strategy_card_versions WHERE symbol = ? "
        "ORDER BY created_at, card_version_id",
        (symbol,),
    ).fetchall()
    print(f"{symbol} 卡片版本 {len(rows)} 个：")
    for r in rows:
        print(f"  {_fmt_row(r)}")
    if not rows:
        print("  （无）")


def cmd_show(conn: sqlite3.Connection, card_version_id: str) -> None:
    row = get_version(conn, card_version_id)
    print(render_card_markdown(row))


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.card")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--cards-root", default=str(CARDS_ROOT),
                        help="卡片 Markdown 根目录（默认 cards/）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-draft", help="校验 JSON 并插入 draft")
    p.add_argument("symbol")
    p.add_argument("--json", required=True, dest="json_path")

    p = sub.add_parser("activate", help="人工确认激活（同事务关闭旧 active）")
    p.add_argument("card_version_id")
    p.add_argument("--effective-from", default=None,
                   help="生效日 YYYY-MM-DD；缺省取换算 draft 的 ex_date，否则当日")

    p = sub.add_parser("reject", help="拒绝 draft / 废止 active")
    p.add_argument("card_version_id")
    p.add_argument("--reason", default=None)

    p = sub.add_parser("list", help="列出该股全部版本")
    p.add_argument("symbol")

    p = sub.add_parser("show", help="显示单个版本（Markdown 渲染）")
    p.add_argument("card_version_id")

    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        if args.cmd == "create-draft":
            card_id = create_draft(conn, args.symbol, args.json_path)
            print(f"draft 已创建: {card_id}（待人工确认激活）")
        elif args.cmd == "activate":
            res = activate(conn, args.card_version_id, args.effective_from,
                           cards_root=Path(args.cards_root))
            print(f"已激活 {res['card_version_id']}（effective_from={res['effective_from']}）")
            if res["superseded"]:
                print(f"旧 active {res['superseded']} 已关闭"
                      f"（superseded，effective_to={res['effective_from']}）")
            print(f"Markdown: {res['version_path']}")
            print(f"current.md 已刷新: {res['current_path']}")
        elif args.cmd == "reject":
            res = reject(conn, args.card_version_id, args.reason,
                         cards_root=Path(args.cards_root))
            print(f"已拒绝 {res['card_version_id']}（原 status={res['was']}）"
                  + (f"：{res['reason']}" if res["reason"] else ""))
        elif args.cmd == "list":
            cmd_list(conn, args.symbol)
        elif args.cmd == "show":
            cmd_show(conn, args.card_version_id)
        return 0
    except CardCLIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: JSON 解析失败: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
