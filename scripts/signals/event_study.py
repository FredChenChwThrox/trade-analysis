"""公告事件研究（设计 §5.5「消息评价与事件研究」保守时点）。

对库内公告事件（events ⋈ event_symbols，event_type='announcement'）做确定性
T+1/T+5 事件研究，结果写 event_assessments
（assessment_version='event_study_v1'，model='deterministic'）：

口径与纪律：

- 交易日历一律用权威来源 trading_calendar（market='CN'）中 is_open 的日期
  升序序列（与 announcement adapter 同一 load_calendar）。日历缺失/为空
  → 全部事件 degraded（calendar_missing，§2.5 不猜）。
- index_bars 000300.SH 只作基准价格来源，不作日历：base 或 T+k 日指数无 bar
  → 按缺失落 degraded（bench_base_missing / bench_missing_tN），不静默顺延。
- 基准价（base）取 available_at 之前最后一个完整交易日：available_at（UTC ISO）
  转 Asia/Shanghai 日期 A（落盘日 00:00 本地语义），base_date = 日历中严格 < A
  的最后一天；T+1 = 日历中 ≥ A 的第 1 天，T+5 = 第 5 天。
- 收盘价一律复权口径：close_raw × price_adj_factor（factor 空按 1.0），
  ret_k = close(T+k)/close(base) − 1，excess_k = ret_k − 同期指数收益。
- 停牌：交易日（权威日历开市）个股无当日 bar → 该终点 null + mark='suspended'，
  不顺延冒充（§5.5）。指数在交易日无 bar 是数据空洞 → degraded，两者不混。
- 数据截止 = 该股 daily_bars 与基准 index_bars 最大 trade_date 的较小者；
  终点日在日历内但 > 数据截止 → null + mark='pending'（尚未到来，非停牌）。
  终点判定顺序：> 数据截止 → pending；个股无 bar → suspended；指数无 bar
  → degraded。
- 数据空洞不猜（§2.5）：base 日个股无 bar / 日历无 A 之前的交易日 / 基准指数
  base 或 T+k 日缺收盘 → status='degraded'，能算的字段照实填、不能算的 null。
- 终点尚未到来（超出日历最大日期，或在日历内但 > 数据截止）→ 该终点 null
  + mark='pending'，事件行仍落库（T+1 能算先算），status 不受影响。
- 事件状态汇总：degraded 优先；任一终点 suspended → 'suspended'；
  仅 pending 不算错误，status='ok'。
- 本模块不冒充 LLM 评价：prompt_version/direction/materiality/confidence/
  rationale 一律 NULL。

幂等：同 (event_id, 'event_study_v1') 行已存在时，仅当 status in ('ok',
'suspended') 且 event_study_json 无 pending 才跳过（suspended 是事实结果，
停牌历史不会改变）；status='degraded'（数据空洞可能在回补后转正）或含
pending → DELETE 该版本行后重算重插（确定性同输入同输出）；绝不动其他
assessment_version 的行。run 记录写 pipeline_runs 阶段
event_study（INSERT OR REPLACE，adapter_version=NULL）。调用方负责事务。

CLI：
    uv run python -m scripts.signals.event_study [--symbol S | --all] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scripts.adapters.common import load_calendar
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.signals.common import RULE_VERSION

ASSESSMENT_VERSION = "event_study_v1"
MODEL = "deterministic"
STAGE = "event_study"
CALENDAR_MARKET = "CN"
CALENDAR_SOURCE = f"trading_calendar:{CALENDAR_MARKET}"
BENCHMARK_INDEX = "000300.SH"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
ENDPOINTS = (("t1", 1), ("t5", 5))


@dataclass
class EventStudyResult:
    symbol: str = "ALL"
    run_id: str = ""
    rule_version: str = RULE_VERSION
    status: str = "ok"           # ok / incomplete
    reason: str = ""
    events_seen: int = 0
    written: int = 0
    recomputed: int = 0          # 含 pending 被 DELETE 重算的行数
    skipped: int = 0             # 已完成（无 pending）跳过的行数
    status_counts: dict = field(default_factory=dict)  # ok/suspended/degraded

    def __str__(self) -> str:
        counts = "、".join(f"{k} {self.status_counts[k]}"
                           for k in ("ok", "suspended", "degraded")
                           if self.status_counts.get(k)) or "无"
        return "\n".join([
            f"{self.symbol} run_id={self.run_id} rule_version={self.rule_version} "
            f"status={self.status}" + (f"（{self.reason}）" if self.reason else ""),
            f"公告事件 {self.events_seen} 条：写入 {self.written} 行"
            f"（其中含 pending 重算 {self.recomputed} 行），"
            f"跳过已完成 {self.skipped} 行",
            f"事件状态分布: {counts}",
        ])


# ---------------------------------------------------------------- 纯函数判定

def local_date_of(available_at: str) -> str:
    """UTC ISO 时间 → Asia/Shanghai 日期（落盘日 00:00 本地语义）。"""
    dt = datetime.fromisoformat(available_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).date().isoformat()


def anchors(calendar: list[str], a_date: str) -> tuple[str | None, int | None]:
    """base_date = 日历中严格 < a_date 的最后一天；t1_idx = 日历中 ≥ a_date
    第一天的下标（无则 None，终点全部 pending）。"""
    base: str | None = None
    t1_idx: int | None = None
    for i, d in enumerate(calendar):
        if d < a_date:
            base = d
        else:
            t1_idx = i
            break
    return base, t1_idx


def _empty_endpoint() -> dict:
    return {"date": None, "close": None, "ret": None, "bench_ret": None,
            "excess": None, "mark": None}


def study_event(calendar: list[str], stock_close: dict[str, float],
                bench_close: dict[str, float], a_date: str,
                data_cutoff: str | None) -> dict:
    """给定日历序列与收盘价映射，算单事件 T+1/T+5 明细（纯函数，可单测）。

    calendar：trading_calendar 开市日升序；stock_close / bench_close：
    trade_date → 复权收盘 / 指数收盘；data_cutoff：数据截止（该股
    daily_bars 与基准 index_bars 最大 trade_date 的较小者，缺失为 None）。
    终点判定顺序：终点日 > data_cutoff → pending（尚未到来，而非停牌）；
    否则个股无 bar → suspended；指数无 bar → degraded。
    返回 dict：base_date、base_close、calendar、benchmark、t1/t5（mark 取值
    None=完整 / 'pending' / 'suspended'）、reason（degraded 原因，无则 None）。
    """
    out: dict = {"base_date": None, "base_close": None, "calendar": CALENDAR_SOURCE,
                 "benchmark": BENCHMARK_INDEX,
                 "t1": _empty_endpoint(), "t5": _empty_endpoint(), "reason": None}
    if not calendar:
        out["reason"] = "calendar_missing"  # 权威日历缺失/为空（§2.5 不猜）
        return out
    base_date, t1_idx = anchors(calendar, a_date)
    out["base_date"] = base_date
    if base_date is None:
        out["reason"] = "no_base_date"      # 日历无 A 之前的交易日
        return out
    base_close = stock_close.get(base_date)
    if base_close is None:
        out["reason"] = "base_no_bar"       # base 日个股无 bar
        return out
    out["base_close"] = base_close
    bench_base = bench_close.get(base_date)
    if bench_base is None:
        out["reason"] = "bench_base_missing"  # 基准指数缺 base 日收盘（数据空洞）
        return out
    for name, k in ENDPOINTS:
        ep = _empty_endpoint()
        if t1_idx is None or t1_idx + k - 1 >= len(calendar):
            ep["mark"] = "pending"          # 终点超出日历（尚未排定）
            out[name] = ep
            continue
        d = calendar[t1_idx + k - 1]
        ep["date"] = d
        if data_cutoff is not None and d > data_cutoff:
            ep["mark"] = "pending"          # 终点日在日历内但尚未到来（非停牌）
            out[name] = ep
            continue
        close = stock_close.get(d)
        if close is None:
            ep["mark"] = "suspended"        # 开市日个股无 bar：停牌
            out[name] = ep
            continue
        ep["close"] = close
        ep["ret"] = close / base_close - 1.0
        bench = bench_close.get(d)
        if bench is None:
            out["reason"] = f"bench_missing_{name}"  # 指数缺终点收盘（数据空洞）
            out[name] = ep
            continue
        ep["bench_ret"] = bench / bench_base - 1.0
        ep["excess"] = ep["ret"] - ep["bench_ret"]
        out[name] = ep
    return out


def study_status(study: dict) -> str:
    """事件状态汇总：degraded 优先 > suspended > ok（仅 pending 不算错误）。"""
    if study.get("reason"):
        return "degraded"
    marks = [study[name]["mark"] for name, _ in ENDPOINTS]
    if "suspended" in marks:
        return "suspended"
    return "ok"


def _has_pending(study: dict) -> bool:
    return any(study.get(name, {}).get("mark") == "pending" for name, _ in ENDPOINTS)


# ---------------------------------------------------------------- 数据加载

def _load_calendar(conn: sqlite3.Connection) -> list[str]:
    """交易日历 = trading_calendar（market=CN）is_open 日期升序（权威来源）。"""
    cal = load_calendar(conn, CALENDAR_MARKET)
    return sorted(d for d, r in cal.items() if r["is_open"])


def _load_bench(conn: sqlite3.Connection) -> dict[str, float]:
    """基准指数收盘映射（000300.SH index_bars；只作价格来源，不作日历）。"""
    rows = conn.execute(
        "SELECT trade_date, close FROM index_bars WHERE index_code = ?",
        (BENCHMARK_INDEX,),
    ).fetchall()
    return {r["trade_date"]: r["close"] for r in rows if r["close"] is not None}


def _load_stock_close(conn: sqlite3.Connection, symbol: str) -> dict[str, float]:
    """复权收盘映射：close_raw × price_adj_factor（factor 空按 1.0）。"""
    rows = conn.execute(
        "SELECT trade_date, close_raw, price_adj_factor FROM daily_bars "
        "WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    return {r["trade_date"]: r["close_raw"] * (r["price_adj_factor"] or 1.0)
            for r in rows if r["close_raw"] is not None}


def _load_events(conn: sqlite3.Connection, symbol: str | None) -> list[dict]:
    sql = """
        SELECT e.event_id, e.event_type, e.available_at, es.symbol
        FROM events e JOIN event_symbols es ON e.event_id = es.event_id
        WHERE e.event_type = 'announcement'
    """
    params: list = []
    if symbol is not None:
        sql += " AND es.symbol = ?"
        params.append(symbol)
    sql += " ORDER BY e.event_id, es.symbol"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- 主流程

def run_event_study(
    conn: sqlite3.Connection,
    symbol: str | None = None,
    *,
    run_id: str | None = None,
) -> EventStudyResult:
    """对公告事件做 T+1/T+5 事件研究并落库（调用方负责事务/提交）。"""
    started_at = utc_now()
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope = symbol or "ALL"
    run_id = run_id or f"event_study_{scope}_{now_compact}"
    res = EventStudyResult(symbol=scope, run_id=run_id)

    calendar = _load_calendar(conn)
    bench = _load_bench(conn)
    bench_max = max(bench) if bench else None
    events = _load_events(conn, symbol)
    stock_cache: dict[str, dict[str, float]] = {}

    for ev in events:
        res.events_seen += 1
        existing = conn.execute(
            "SELECT status, event_study_json FROM event_assessments "
            "WHERE event_id = ? AND assessment_version = ?",
            (ev["event_id"], ASSESSMENT_VERSION),
        ).fetchone()
        if existing is not None:
            try:
                old = json.loads(existing["event_study_json"] or "{}")
            except json.JSONDecodeError:
                old = {}
            # 仅完整行跳过（ok/suspended 且无 pending）；degraded（数据空洞
            # 可能回补转正）或含 pending → DELETE 本版本行后重算重插
            if existing["status"] in ("ok", "suspended") and not _has_pending(old):
                res.skipped += 1
                continue
            conn.execute(
                "DELETE FROM event_assessments "
                "WHERE event_id = ? AND assessment_version = ?",
                (ev["event_id"], ASSESSMENT_VERSION),
            )
            res.recomputed += 1

        sym = ev["symbol"]
        if sym not in stock_cache:
            stock_cache[sym] = _load_stock_close(conn, sym)
        if not ev["available_at"]:
            study = {"base_date": None, "base_close": None,
                     "calendar": CALENDAR_SOURCE, "benchmark": BENCHMARK_INDEX,
                     "t1": _empty_endpoint(), "t5": _empty_endpoint(),
                     "reason": "missing_available_at"}
        else:
            closes = stock_cache[sym]
            # 数据截止 = 该股 daily_bars 与基准 index_bars 最大日的较小者
            # （任一缺失则相应 degraded：base_no_bar / bench_base_missing 会先命中）
            stock_max = max(closes) if closes else None
            cutoff = (min(stock_max, bench_max)
                      if stock_max and bench_max else None)
            study = study_event(calendar, closes, bench,
                                local_date_of(ev["available_at"]), cutoff)
        status = study_status(study)
        study["run_id"] = run_id
        conn.execute(
            """
            INSERT INTO event_assessments (event_id, assessment_version, model,
                prompt_version, assessed_at, event_type, direction, materiality,
                confidence, rationale, status, event_study_json, run_id)
            VALUES (?, ?, ?, NULL, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)
            """,
            (ev["event_id"], ASSESSMENT_VERSION, MODEL, utc_now(),
             ev["event_type"], status,
             json.dumps(study, ensure_ascii=False, sort_keys=True), run_id),
        )
        res.written += 1
        res.status_counts[status] = res.status_counts.get(status, 0) + 1

    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, app_version,
            status, error, started_at, finished_at)
        VALUES (?, 'event_study', ?, ?, NULL, NULL, ?, NULL, 'success', NULL, ?, ?)
        """,
        (run_id, utc_now(), calendar[-1] if calendar else None,
         RULE_VERSION, started_at, utc_now()),
    )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.signals.event_study")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--symbol", default=None, help="只处理该 symbol 的公告事件")
    group.add_argument("--all", action="store_true",
                       help="全部有公告事件的 symbol（默认）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        with conn:  # 幂等写 + run 记录同一事务
            res = run_event_study(conn, symbol=args.symbol)
        print(res)
        return 0 if res.status == "ok" else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
