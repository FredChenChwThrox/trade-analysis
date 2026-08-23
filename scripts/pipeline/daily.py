"""每日盘后 pipeline（D1.8 + D2.6 接入，设计 §8.1、§2.5 失败原则、§8.3 幂等）。

覆盖 §8.1 的 1-5 步、步骤 6 的确定性部分与 7 步（LLM 消息评价本版不含）：
1. 按 trading_calendar 判定该日各市场状态，创建 pipeline_run（§2.3 版本字段全填）；
2. --raw-dir 中的新文件 ingest 入库（raw content hash 去重，§8.3）；
3. 对 watchlist 每只股票跑日历门禁（calendar_check）；
4. 因子变化检查（adjust 重叠窗口逻辑，raw-dir 中有该股 *_forward* 文件时）；
5. 周线/指标全量重算（weekly/compute），随后依次重算信号：
   weekly_signals → daily_watch → right_side → accumulation → corporate_action
   检测（§8.1 步骤 5，信号阶段各自独立事务）；
6. 池级事件研究（event_study，§5.5 确定性部分）：全部股票处理完后对库内
   公告事件跑 T+1/T+5 研究写 event_assessments（event_study_v1）；
7. 生成单股报告 + 全池日报（report.run_reports，§8.1 步骤 7）。

契约：
- 原子性：单只股票的"入库→因子→周线→指标"基础阶段在一个事务内；失败回滚并
  标记该股 failed，不影响其他股票（§8.1：保留上一次成功版本供查询）。
- 信号阶段事务边界：每个信号模块在独立子事务内顺序执行；单模块异常只回滚该
  模块（不残留部分写入）、记 notes 标 degraded、状态记 incomplete 并 break
  （后续模块不跑，避免基于前序失败留下的旧派生数据继续判定）；已成功模块的
  提交保留（信号为派生数据可重算，§2.2 第 3 类）。
- 报告阶段失败不阻断前面阶段：异常被捕获，pipeline_runs 阶段 report 记 degraded。
- 事件研究阶段失败不阻断报告阶段：异常被捕获记 notes 并以 _record_stage 记
  daily 台账 stage='event_study' degraded（派生数据可重算，§2.2 第 3 类）；
  run_event_study 自身另记一条其 run_id 的 pipeline_runs（与 accumulation
  双记录模式一致）。
- 幂等：同一 date 重跑安全——run_id 固定为 daily_{date}，pipeline_runs 同 run_id
  覆盖阶段状态；raw content hash 不重复解析；指标/周线/信号 DELETE+重插；
  报告按 §9.5 降级生成 revision 新行（report_runs 随重跑增长是设计行为）。
- 非交易日：明确输出 non_trading_day 并跳过计算，不报错。
- 门禁 incomplete（日历缺失/超范围）或 source_missing：输出 incomplete 及原因，
  跳过该股计算，不产出"条件满足"（§2.5）。

CLI：
    uv run python -m scripts.pipeline.daily --date 2026-08-07 [--raw-dir PATH] [--db PATH]
    uv run python -m scripts.pipeline.daily status 603605.SH [--date D] [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date as date_type
from pathlib import Path

import pandas as pd

from scripts.adapters.common import (
    ADAPTER_VERSION,
    BatchRejected,
    IngestResult,
    ingest_file,
    load_calendar,
    market_of,
    register_and_parse,
    sha256_file,
)
from scripts.indicators import core
from scripts.indicators import compute as ind_compute
from scripts.pipeline import ingest as ingest_mod
from scripts.pipeline.adjust import apply_adjustment, check_factor_change
from scripts.pipeline.calendar_check import (
    STATUS_INCOMPLETE,
    STATUS_NON_TRADING,
    STATUS_SOURCE_MISSING,
    STATUS_SUSPENDED,
    check_symbol_day,
)
from scripts.pipeline.db import DEFAULT_DB_PATH, ROOT, connect, utc_now
from scripts.pipeline.weekly import rebuild_weekly
from scripts.pipeline import report as report_mod
from scripts.signals import accumulation as acc_mod
from scripts.signals import corporate_action as ca_mod
from scripts.signals import daily_watch as dw_mod
from scripts.signals import event_study as es_mod
from scripts.signals import right_side as rs_mod
from scripts.signals import weekly_signals as ws_mod

ST_OK = "ok"
ST_NON_TRADING = "non_trading_day"
ST_INCOMPLETE = "incomplete"
ST_SUSPENDED = "suspended"
ST_FAILED = "failed"


# ---------------------------------------------------------------- 结果结构

@dataclass
class SymbolResult:
    symbol: str
    status: str
    reason: str = ""
    gate: str = ""
    ingest: list[IngestResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = f"{self.symbol}: {self.status}" + (f"（{self.reason}）" if self.reason else "")
        for n in self.notes:
            s += f"\n  NOTE: {n}"
        return s


@dataclass
class DailyResult:
    trade_date: str
    run_id: str
    markets: dict[str, str] = field(default_factory=dict)  # market -> 状态描述
    symbols: list[SymbolResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_failed(self) -> bool:
        return any(s.status == ST_FAILED for s in self.symbols)

    @property
    def has_incomplete(self) -> bool:
        return any(s.status == ST_INCOMPLETE for s in self.symbols)

    def __str__(self) -> str:
        lines = [f"daily run {self.run_id} (date={self.trade_date})"]
        lines.extend(f"  市场 {m}: {s}" for m, s in sorted(self.markets.items()))
        lines.extend(f"  {s}" for s in self.symbols)
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        counts: dict[str, int] = {}
        for s in self.symbols:
            counts[s.status] = counts.get(s.status, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "无股票"
        lines.append(f"  汇总: {summary}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 版本与运行记录（§2.3）

def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=ROOT, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _record_stage(conn: sqlite3.Connection, run_id: str, stage: str, trade_date: str,
                  status: str, *, error: str | None, started_at: str,
                  config_hash: str, git_commit: str | None) -> None:
    """写 pipeline_runs 阶段行（同 run_id+stage 覆盖，幂等重跑 §8.3）。"""
    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, card_version_id,
            app_version, git_commit, status, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, stage, utc_now(), trade_date, ADAPTER_VERSION, config_hash,
         core.RULE_VERSION, f"pandas {pd.__version__}", git_commit,
         status, error, started_at, utc_now()),
    )


# ---------------------------------------------------------------- 市场状态

def market_day_status(conn: sqlite3.Connection, market: str, trade_date: str) -> tuple[str, str]:
    """该市场当日状态：("open"|"closed"|"missing", 原因)。"""
    calendar = load_calendar(conn, market)
    if not calendar:
        return "missing", f"trading_calendar 缺失（market={market}），无法判定（§2.5 不猜）"
    cal = calendar.get(trade_date)
    if cal is None:
        return "missing", f"{trade_date} 超出 trading_calendar 种子范围（market={market}）"
    if not cal["is_open"]:
        return "closed", f"休市（{cal['status_detail'] or cal['status']}）"
    return "open", ""


# ---------------------------------------------------------------- raw 文件分类

def _classify_raw_files(
    raw_dir: str,
    watchlist: set[str],
) -> tuple[dict[str, list[Path]], dict[str, list[Path]], list[Path], list[Path]]:
    """把 raw-dir 下 CSV 分为：watchlist 个股文件 / 个股 forward 文件 / 其他可路由文件 / 无法路由。

    forward 文件（price 目录下 *_forward*）不入 daily_bars，留给因子变化检查（§3.3）。
    """
    by_symbol: dict[str, list[Path]] = {}
    forward_by_symbol: dict[str, list[Path]] = {}
    other: list[Path] = []
    unrouted: list[Path] = []
    for path in ingest_mod.iter_csv_files([raw_dir]):
        route = ingest_mod._route(path)
        if route is None or route not in ingest_mod._ROUTES:
            unrouted.append(path)
            continue
        source, data_type = route
        if data_type == "price" and "_forward" in path.stem:
            symbol = path.stem.split("_forward")[0]
            if symbol in watchlist:
                forward_by_symbol.setdefault(symbol, []).append(path)
            else:
                other.append(path)  # 非 watchlist 的 forward 文件本流程不处理
            continue
        symbol = ingest_mod._symbol_from_filename(path)
        if symbol and symbol in watchlist:
            by_symbol.setdefault(symbol, []).append(path)
        else:
            other.append(path)
    return by_symbol, forward_by_symbol, other, unrouted


# ---------------------------------------------------------------- 单股处理（原子事务）

def _ingest_symbol_files(
    conn: sqlite3.Connection,
    files: list[Path],
    symbol: str,
) -> list[IngestResult]:
    """在当前事务内入库该股文件（content hash 命中自动跳过；校验失败抛 BatchRejected）。"""
    results = []
    for path in sorted(files):
        source, data_type = ingest_mod._route(path)
        results.append(register_and_parse(
            conn, path, source=source, data_type=data_type, symbol=symbol,
            parse=ingest_mod._ROUTES[(source, data_type)],
        ))
    return results


def _process_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    trade_date: str,
    run_id: str,
    files: list[Path],
    forward_files: list[Path],
) -> SymbolResult:
    """单股：门禁 →（事务：入库→因子→周线→指标）→（信号阶段各自独立事务）。

    基础阶段任一失败整体回滚标 failed（§8.1）；信号阶段顺序执行，单阶段异常
    只回滚该阶段、后续阶段跳过、标 incomplete（派生数据可重算，§2.2 第 3 类）。
    """
    market = market_of(symbol)
    mstatus, mreason = market_day_status(conn, market, trade_date)
    if mstatus == "missing":
        return SymbolResult(symbol, ST_INCOMPLETE, reason=mreason)
    if mstatus == "closed":
        return SymbolResult(symbol, ST_NON_TRADING, reason=mreason)

    res = SymbolResult(symbol, ST_OK)
    try:
        with conn:  # 原子：入库→因子→周线→指标，任一失败整体回滚
            res.ingest = _ingest_symbol_files(conn, files, symbol)
            for r in res.ingest:
                if r.skipped and not r.inserted and not r.updated:
                    res.notes.append(f"{Path(r.file_path).name}: content hash 已登记，跳过")

            gate = check_symbol_day(conn, symbol, trade_date)
            res.gate = gate.status
            if gate.status in (STATUS_INCOMPLETE, STATUS_SOURCE_MISSING, STATUS_NON_TRADING):
                # 入库数据保留（已校验合法），计算跳过（§2.5）
                res.status = ST_INCOMPLETE
                res.reason = gate.reason or gate.status
                return res

            # ---- 因子变化检查（有 forward 文件时，§3.3 重叠窗口）
            rebuilt = False
            if forward_files:
                fwd = sorted(forward_files)[-1]
                chk = check_factor_change(conn, symbol, fwd)
                if chk.changed:
                    adj = apply_adjustment(
                        conn, symbol, forward_csv=fwd,
                        run_id=f"{run_id}_adjust_{symbol}")
                    res.notes.append(
                        f"因子变化（{chk.reason}）→ 全量重建因子+周线，"
                        f"version_id={adj.version_id}")
                    rebuilt = True  # apply_adjustment 已同事务重建周线
                else:
                    res.notes.append(f"因子一致（{chk.reason}）")
            # ---- 周线 + 指标全量重算（§4.3）
            if not rebuilt:
                wk = rebuild_weekly(conn, symbol, run_id=f"{run_id}_weekly_{symbol}")
                if not wk.weeks_written:
                    res.notes.append(f"周线未写入: {'; '.join(wk.notes) or '无完成周'}")
            comp = ind_compute.recompute_indicators(
                conn, symbol, run_id=f"{run_id}_ind_{symbol}")
            res.notes.append(
                f"指标重算 daily={comp.daily_rows} weekly={comp.weekly_rows} "
                f"pe_ttm 非空={comp.pe_ok} 空={comp.pe_null}")
    except BatchRejected as reject:
        res.status = ST_FAILED
        res.reason = "入库校验失败，该股当日全部阶段回滚: " + (
            "; ".join(reject.result.errors) or str(reject))
        return res
    except Exception as exc:  # 单股失败不影响其他股票（§8.1）
        res.status = ST_FAILED
        res.reason = f"{type(exc).__name__}: {exc}（该股当日全部阶段回滚）"
        return res

    # ---- 信号阶段（§8.1 步骤 5）：weekly_signals → daily_watch →
    # right_side → accumulation → corporate_action 检测。每阶段独立事务：
    # 单阶段异常只回滚该阶段、记 degraded 并 break（后续阶段不跑，避免基于
    # 前序失败留下的旧派生数据继续判定）；已成功阶段的提交保留（§2.2 第 3 类）。
    signal_stages = (
        ("weekly_signals", lambda: ws_mod.recompute_weekly_signals(
            conn, symbol, run_id=f"{run_id}_weekly_signals_{symbol}")),
        ("daily_watch", lambda: dw_mod.run_daily_watch(
            conn, symbol, as_of=trade_date,
            run_id=f"{run_id}_daily_watch_{symbol}")),
        ("right_side", lambda: rs_mod.run_right_side(
            conn, symbol, as_of=trade_date,
            run_id=f"{run_id}_right_side_{symbol}")),
        ("accumulation", lambda: acc_mod.run_accumulation(
            conn, symbol, as_of=trade_date,
            run_id=f"{run_id}_accumulation_{symbol}")),
        ("corporate_action", lambda: ca_mod.process_pending(
            conn, symbol, as_of=trade_date,
            run_id=f"{run_id}_corporate_action_{symbol}")),
    )
    for stage_name, fn in signal_stages:
        try:
            with conn:  # 阶段独立事务：异常回滚本阶段，不残留部分写入
                r = fn()
            res.notes.append(f"信号 {stage_name}: {getattr(r, 'status', 'ok')}"
                             + (f"（{r.reason}）" if getattr(r, 'reason', '') else ""))
        except Exception as exc:
            res.notes.append(
                f"信号 {stage_name} degraded: {type(exc).__name__}: {exc}"
                f"（该阶段已回滚，后续信号阶段跳过）")
            res.status = ST_INCOMPLETE
            res.reason = f"{stage_name}_failed"
            break

    if gate.status == STATUS_SUSPENDED and res.status == ST_OK:
        res.status = ST_SUSPENDED
        res.reason = gate.reason
    return res


# ---------------------------------------------------------------- 主流程

def run_daily(
    conn: sqlite3.Connection,
    trade_date: str,
    raw_dir: str | None = None,
    reports_root: str | None = None,
) -> DailyResult:
    """每日盘后 pipeline（§8.1 步骤 1-5、7）。trade_date 为市场本地日期 YYYY-MM-DD。

    reports_root 为 None 时用 report.REPORTS_ROOT（reports/）；测试传临时目录。
    """
    date_type.fromisoformat(trade_date)  # 格式校验
    run_id = f"daily_{trade_date}"
    config_hash = sha256_file(ind_compute.INDICATORS_CONFIG)
    git_commit = _git_commit()
    result = DailyResult(trade_date=trade_date, run_id=run_id)

    watch = conn.execute(
        "SELECT symbol, market FROM watchlist WHERE active = 1 ORDER BY symbol",
    ).fetchall()
    symbols = [r["symbol"] for r in watch]

    # ---- 步骤 1：市场状态 + pipeline_run
    started = utc_now()
    for market in sorted({r["market"] for r in watch}):
        mstatus, mreason = market_day_status(conn, market, trade_date)
        result.markets[market] = {"open": "交易日", "closed": f"non_trading_day（{mreason}）",
                                  "missing": f"incomplete（{mreason}）"}[mstatus]
    cal_status = "degraded" if any("incomplete" in s for s in result.markets.values()) else "success"
    with conn:
        _record_stage(conn, run_id, "calendar", trade_date, cal_status,
                      error="; ".join(f"{m}: {s}" for m, s in sorted(result.markets.items())),
                      started_at=started, config_hash=config_hash, git_commit=git_commit)

    # ---- 步骤 2：ingest 分类（watchlist 个股文件随各股事务入库，其余先行入库）
    by_symbol: dict[str, list[Path]] = {s: [] for s in symbols}
    forward_by_symbol: dict[str, list[Path]] = {}
    if raw_dir:
        by_symbol, forward_by_symbol, other, unrouted = _classify_raw_files(
            raw_dir, set(symbols))
        for s in symbols:
            by_symbol.setdefault(s, [])
        for path in other:  # 非 watchlist 文件（指数/fx 等）：单文件事务入库
            source, data_type = ingest_mod._route(path)
            r = ingest_file(conn, path, source=source, data_type=data_type,
                            symbol=ingest_mod._symbol_from_filename(path),
                            parse=ingest_mod._ROUTES[(source, data_type)])
            result.notes.append(f"其他文件 {path.name}: {r.summary()}")
        for path in unrouted:
            result.notes.append(f"无法路由，跳过: {path}")

    # ---- 步骤 3-5：逐股门禁 + 原子计算
    for symbol in symbols:
        started = utc_now()
        sr = _process_symbol(
            conn, symbol, trade_date, run_id,
            by_symbol.get(symbol, []), forward_by_symbol.get(symbol, []),
        )
        result.symbols.append(sr)
        stage_status = {
            ST_OK: "success", ST_SUSPENDED: "success", ST_NON_TRADING: "success",
            ST_INCOMPLETE: "degraded", ST_FAILED: "failed",
        }[sr.status]
        with conn:
            _record_stage(conn, run_id, f"symbol:{symbol}", trade_date, stage_status,
                          error=sr.reason or None, started_at=started,
                          config_hash=config_hash, git_commit=git_commit)

    # ---- 步骤 6（确定性部分）：池级事件研究（§5.5；LLM 消息评价本版不含）。
    # 派生数据可重算（§2.2 第 3 类）：失败记 degraded 不阻断报告阶段。
    started = utc_now()
    try:
        with conn:  # 池级事务：事件落库 + run_event_study 自记的 pipeline_runs 同事务
            es_res = es_mod.run_event_study(conn, run_id=f"{run_id}_event_study")
        es_counts = "、".join(
            f"{k} {es_res.status_counts[k]}"
            for k in ("ok", "suspended", "degraded")
            if es_res.status_counts.get(k)) or "无"
        result.notes.append(
            f"事件研究 event_study: 公告事件 {es_res.events_seen} 条，"
            f"写入 {es_res.written} 行（含 pending 重算 {es_res.recomputed}），"
            f"跳过已完成 {es_res.skipped} 行；状态分布: {es_counts}")
        es_status, es_error = "success", None
    except Exception as exc:
        es_status = "degraded"
        es_error = f"{type(exc).__name__}: {exc}"
        result.notes.append(f"event_study degraded: {es_error}")
    with conn:
        _record_stage(conn, run_id, "event_study", trade_date, es_status,
                      error=es_error, started_at=started,
                      config_hash=config_hash, git_commit=git_commit)

    # ---- 步骤 7：报告生成（单股 + 全池日报；失败不阻断前面阶段，记 degraded）
    started = utc_now()
    try:
        rep = report_mod.run_reports(
            conn, trade_date, reports_root=reports_root, run_id=run_id,
            indicators_config_hash=config_hash)
        for s in rep.symbols:
            result.notes.append(
                f"报告 {s.symbol}: {s.status} P{s.priority} revision={s.revision}")
        result.notes.append(f"全池日报 revision={rep.daily_revision} -> {rep.daily_path}")
        result.notes.extend(rep.notes)
        report_status = ("degraded" if any(
            s.status != "complete" for s in rep.symbols) else "success")
        report_error = None
    except Exception as exc:  # 报告阶段失败不阻断前面阶段（§8.1）
        report_status, report_error = "degraded", f"{type(exc).__name__}: {exc}"
        result.notes.append(f"报告阶段 degraded: {report_error}")
    with conn:
        _record_stage(conn, run_id, "report", trade_date, report_status,
                      error=report_error, started_at=started,
                      config_hash=config_hash, git_commit=git_commit)

    # ---- 汇总
    started = utc_now()
    overall = "failed" if result.has_failed else (
        "degraded" if result.has_incomplete else "success")
    counts: dict[str, int] = {}
    for s in result.symbols:
        counts[s.status] = counts.get(s.status, 0) + 1
    with conn:
        _record_stage(conn, run_id, "summary", trade_date, overall,
                      error=", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or None,
                      started_at=started, config_hash=config_hash, git_commit=git_commit)
    return result


# ---------------------------------------------------------------- 状态查询

def symbol_status(conn: sqlite3.Connection, symbol: str,
                  as_of: str | None = None) -> int:
    """打印该股最近 5 个交易日的门禁状态与指标可用性（供报告层用）。"""
    market = market_of(symbol)
    calendar = load_calendar(conn, market)
    if not calendar:
        print(f"{symbol}: incomplete（trading_calendar 缺失 market={market}，§2.5）")
        return 2
    if as_of is None:
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars WHERE symbol = ?", (symbol,),
        ).fetchone()
        as_of = row["d"] or date_type.today().isoformat()
    open_days = sorted(d for d, r in calendar.items() if r["is_open"] and d <= as_of)[-5:]
    if not open_days:
        print(f"{symbol}: {as_of} 之前无交易日（日历范围不足）")
        return 2

    print(f"{symbol} 最近 {len(open_days)} 个交易日（截至 {as_of}）：")
    for d in open_days:
        gate = check_symbol_day(conn, symbol, d)
        ind = conn.execute(
            "SELECT pe_ttm, pe_status, computed_at FROM indicators_daily "
            "WHERE symbol = ? AND trade_date = ?", (symbol, d),
        ).fetchone()
        run = conn.execute(
            "SELECT status, error FROM pipeline_runs WHERE run_id = ? AND stage = ?",
            (f"daily_{d}", f"symbol:{symbol}"),
        ).fetchone()
        if ind is None:
            ind_s = "指标: 无行"
        elif ind["pe_ttm"] is not None:
            ind_s = f"指标: ok, pe_ttm={ind['pe_ttm']:.4f}（{ind['pe_status']}）"
        else:
            ind_s = f"指标: pe_ttm=NULL（{ind['pe_status']}）"
        run_s = (f"daily_run={run['status']}" + (f"（{run['error']}）" if run["error"] else "")
                 ) if run else "daily_run=—"
        gate_s = gate.status + (f"（{gate.reason}）" if gate.reason else "")
        print(f"  {d}  gate={gate_s}  {ind_s}  {run_s}")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "status":
        parser = argparse.ArgumentParser(prog="scripts.pipeline.daily status")
        parser.add_argument("symbol")
        parser.add_argument("--date", default=None, help="截止日期，默认该股最新 bar 日")
        parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
        args = parser.parse_args(argv[1:])
        conn = connect(args.db)
        try:
            return symbol_status(conn, args.symbol, args.date)
        finally:
            conn.close()

    parser = argparse.ArgumentParser(prog="scripts.pipeline.daily")
    parser.add_argument("--date", required=True, help="交易日期 YYYY-MM-DD（市场本地）")
    parser.add_argument("--raw-dir", default=None, help="本批新采 raw 文件目录（可选）")
    parser.add_argument("--reports-root", default=None,
                        help="报告根目录（默认 reports/；测试用临时目录）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        result = run_daily(conn, args.date, raw_dir=args.raw_dir,
                           reports_root=args.reports_root)
    finally:
        conn.close()
    print(result)
    if result.has_failed:
        return 1
    if result.has_incomplete:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
