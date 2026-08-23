"""复权因子模块（D1.5，设计 §3.3）。

口径与算法：
- 来源只提供前复权价（adjust=forward，锚定最新价）：来源因子 f_t = 前复权收盘 ÷ 不复权收盘。
  两侧价格均为 2 位小数，f_t 有 ±0.0001 级舍入噪声（probe01 实测，
  见 docs/probe_20260809_stock_finance_data.md），必须做平台段检测：
  以当前段内中位数为参考，相对偏差 <= 0.1% 归入同一段，否则开新段。
  不能按逐日 diff 判除权。
- 内部前向累积因子 price_adj_factor_t = f_t / f_origin，
  origin = 该股库内最早交易日（归一 1.0），复权价 = raw × factor
  （后复权序列：历史值稳定，新除权只改变之后的因子）。
- share_factor_t = 当时股本 ÷ 最新股本（历史股本较少时 < 1，
  raw / share_factor 把历史量放大到当前股本口径），只反映拆股/送转
  （corporate_actions.split_ratio），现金分红不含；无送转时全为 1.0。
  出现送转/拆股事件时按倍率填并输出 TODO 提示人工核对。
- 因子列更新（daily_bars.price_adj_factor / share_factor）与周线重建在同一事务
  （派生数据重算，§2.2 第 3 类）；不复权 OHLC 一律不动（原始事实）。
- 每次重建写 adjustment_factor_versions（算法、origin、来源、平台段明细）。

增量（§3.3）：重叠窗口（≥5 个交易日）新采前复权与库内因子比对，
相对位移 > 0.1% 判定因子变化 → 触发该股全量重建，不得只改最后几日。

CLI：
    uv run python -m scripts.pipeline.adjust <symbol> --forward-csv PATH [--db PATH]
    uv run python -m scripts.pipeline.adjust <symbol> --forward-csv PATH --check-only
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from scripts.adapters.common import load_calendar, sha256_file
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now
from scripts.pipeline.weekly import WeeklyResult, rebuild_weekly

TOL_REL = 0.001  # 平台段检测 / 因子变化检测阈值（0.1% 相对，probe01 建议值）
ALGORITHM = "forward_over_none_plateau_v1"
MAX_CA_LAG_TD = 2  # 平台切换日与 corporate_actions 除权日允许偏差（交易日）

_SPLIT_TYPES = ("split", "bonus_share")


# ---------------------------------------------------------------- 数据结构

@dataclass
class Plateau:
    """来源因子平台段：起止交易日 + 段内中位数因子值。"""

    start: str
    end: str
    factor: float

    def __str__(self) -> str:
        return f"{self.start}~{self.end}: f={self.factor:.6f}"


@dataclass
class FactorChangeResult:
    """重叠窗口因子变化检测结果。"""

    changed: bool
    reason: str
    n_overlap: int = 0
    median_ratio: float | None = None  # f_new/internal 的中位数（= 新口径下的 f_origin）
    reference: float | None = None     # 版本记录中的 f_origin 基准

    def __str__(self) -> str:
        tag = "CHANGED" if self.changed else "OK"
        return (f"[{tag}] overlap={self.n_overlap} median_ratio={self.median_ratio} "
                f"reference={self.reference}: {self.reason}")


@dataclass
class AdjustResult:
    symbol: str = ""
    run_id: str = ""
    origin_date: str = ""
    plateaus: list[Plateau] = field(default_factory=list)
    switch_dates: list[str] = field(default_factory=list)
    bars_updated: int = 0
    bars_kept: int = 0
    version_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    weekly: WeeklyResult | None = None

    def __str__(self) -> str:
        lines = [
            f"{self.symbol} origin={self.origin_date} run_id={self.run_id}",
            f"平台段 {len(self.plateaus)} 段，切换日 {self.switch_dates}",
            f"daily_bars 因子更新 {self.bars_updated} 行，保留 {self.bars_kept} 行，"
            f"version_id={self.version_id}",
        ]
        lines.extend(f"  {p}" for p in self.plateaus)
        lines.extend(f"  WARNING: {w}" for w in self.warnings)
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        if self.weekly is not None:
            lines.append(f"  周线（同事务重建）: {self.weekly}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 纯函数：因子与平台段

def load_forward_closes(path: str | Path) -> dict[str, float]:
    """读取前复权 CSV 的收盘价 {trade_date: close}（stock_finance_data 行情格式）。"""
    closes: dict[str, float] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            raw_time = (rec.get("time") or "").strip()
            raw_close = (rec.get("close") or "").strip()
            if not raw_time or not raw_close:
                continue
            if "-" in raw_time:
                trade_date = raw_time[:10]
            else:
                trade_date = datetime.strptime(raw_time, "%Y%m%d").date().isoformat()
            closes[trade_date] = float(raw_close)
    return closes


def compute_source_factor(
    raw_closes: dict[str, float],
    fwd_closes: dict[str, float],
) -> dict[str, float]:
    """来源因子 f_t = 前复权收盘 ÷ 不复权收盘（取两边交集日期）。"""
    f: dict[str, float] = {}
    for d in sorted(set(raw_closes) & set(fwd_closes)):
        raw = raw_closes[d]
        if raw:
            f[d] = fwd_closes[d] / raw
    return f


def detect_plateaus(f: dict[str, float], tol: float = TOL_REL) -> list[Plateau]:
    """把 f_t 分段为平台：以当前段中位数为参考，相对偏差 > tol 开新段。

    段因子取段内中位数，免疫 ±0.0001 级舍入噪声（probe01）。
    """
    dates = sorted(f)
    if not dates:
        return []
    plateaus: list[Plateau] = []
    seg: list[float] = [f[dates[0]]]
    seg_start = dates[0]
    prev = dates[0]
    for d in dates[1:]:
        ref = median(seg)
        v = f[d]
        if ref and abs(v - ref) / abs(ref) > tol:
            plateaus.append(Plateau(seg_start, prev, median(seg)))
            seg = [v]
            seg_start = d
        else:
            seg.append(v)
        prev = d
    plateaus.append(Plateau(seg_start, prev, median(seg)))
    return plateaus


def plateau_series(f: dict[str, float], plateaus: list[Plateau]) -> dict[str, float]:
    """每个交易日映射到所属平台段的中位数因子（平坦化，去掉日级噪声）。"""
    series: dict[str, float] = {}
    for p in plateaus:
        for d in f:
            if p.start <= d <= p.end:
                series[d] = p.factor
    return series


def normalize_factors(
    series: dict[str, float],
    origin_date: str,
) -> dict[str, float]:
    """归一化为内部前向累积因子：factor_t = f_t / f_origin（origin 日 = 1.0）。"""
    if origin_date not in series:
        raise ValueError(f"origin 日 {origin_date} 不在因子序列内")
    f_origin = series[origin_date]
    return {d: v / f_origin for d, v in series.items()}


def compute_share_factors(
    dates: list[str],
    split_events: list[tuple[str, float]],
) -> tuple[dict[str, float], list[str]]:
    """share_factor_t = Π(1/ratio_i)（ex_date > t 的送转/拆股事件）。

    即当时股本 ÷ 最新股本：10 送 10（ratio=2）之前的日子 factor=0.5，
    volume_raw / 0.5 把历史量放大到当前股本口径（§3.3 防虚假放量）。
    返回（逐日因子, TODO 提示清单）。
    """
    factors: dict[str, float] = {}
    for d in sorted(dates):
        v = 1.0
        for ex_date, ratio in split_events:
            if ex_date > d:
                v /= ratio
        factors[d] = v
    todos = [
        f"TODO: 送转/拆股事件 {ex} 倍率 {ratio:g} 已计入 share_factor"
        f"（生效日 {ex} 之前的历史量 ÷ {1 / ratio:g} 换算到当前股本口径），请人工核对股本"
        for ex, ratio in split_events
    ]
    return factors, todos


# ---------------------------------------------------------------- 因子变化检测（增量用）

def detect_factor_change(
    internal: dict[str, float],
    f_new: dict[str, float],
    f_origin_ref: float | None,
    tol: float = TOL_REL,
) -> FactorChangeResult:
    """重叠窗口因子变化检测（§3.3 增量）。

    internal：库内 price_adj_factor（旧版本，origin 归一）；
    f_new：新采重叠窗口的来源因子（forward ÷ none）；
    f_origin_ref：当前版本 notes 中记录的来源因子基准。

    原理：internal_t = f_old_t / f_origin，故 r_t = f_new_t / internal_t
    在因子未变时恒等于 f_origin（常数）；新除权使整段历史前移一个平台，
    r_t 整体位移（或窗口内出现两个平台）即判定变化 → 触发全量重建。
    """
    common = sorted(set(internal) & set(f_new))
    if not common:
        return FactorChangeResult(True, "重叠窗口为空，无法比对，按变化处理（触发全量重建）",
                                  reference=f_origin_ref)
    ratios = {d: f_new[d] / internal[d] for d in common if internal[d]}
    if not ratios:
        return FactorChangeResult(True, "库内因子为 0，无法比对，按变化处理",
                                  reference=f_origin_ref)
    med = median(ratios.values())
    spread = max(abs(r - med) / abs(med) for r in ratios.values())
    if spread > tol:
        return FactorChangeResult(
            True,
            f"重叠窗口内出现平台段位移（max_dev={spread:.4%} > {tol:.1%}），"
            f"除权落在窗口内，触发全量重建",
            n_overlap=len(ratios), median_ratio=med, reference=f_origin_ref)
    if f_origin_ref is None:
        return FactorChangeResult(
            True, "无因子版本基准（首次或版本缺失），触发全量重建",
            n_overlap=len(ratios), median_ratio=med, reference=f_origin_ref)
    shift = abs(med - f_origin_ref) / abs(f_origin_ref)
    if shift > tol:
        return FactorChangeResult(
            True,
            f"来源因子整体位移 {shift:.4%} > {tol:.1%}（新除权），触发全量重建",
            n_overlap=len(ratios), median_ratio=med, reference=f_origin_ref)
    return FactorChangeResult(
        False, f"因子一致（位移 {shift:.4%} <= {tol:.1%}）",
        n_overlap=len(ratios), median_ratio=med, reference=f_origin_ref)


def check_factor_change(
    conn: sqlite3.Connection,
    symbol: str,
    forward_csv: str | Path,
    tol: float = TOL_REL,
) -> FactorChangeResult:
    """库内因子 + 新采 forward 重叠窗口 CSV → 因子变化检测（增量入口）。"""
    rows = conn.execute(
        "SELECT trade_date, close_raw, price_adj_factor FROM daily_bars "
        "WHERE symbol = ? ORDER BY trade_date", (symbol,),
    ).fetchall()
    internal = {r["trade_date"]: r["price_adj_factor"] for r in rows}
    raw_closes = {r["trade_date"]: r["close_raw"] for r in rows}
    fwd = load_forward_closes(forward_csv)
    f_new = compute_source_factor(raw_closes, fwd)
    ref = _latest_f_origin_ref(conn, symbol)
    return detect_factor_change(internal, f_new, ref, tol)


def _latest_f_origin_ref(conn: sqlite3.Connection, symbol: str) -> float | None:
    row = conn.execute(
        "SELECT notes FROM adjustment_factor_versions WHERE symbol = ? "
        "ORDER BY version_id DESC LIMIT 1", (symbol,),
    ).fetchone()
    if row is None or not row["notes"]:
        return None
    try:
        notes = json.loads(row["notes"])
    except json.JSONDecodeError:
        return None
    return notes.get("source_factor_at_origin")


# ---------------------------------------------------------------- 交叉印证

def _trading_day_distance(open_days: list[str], a: str, b: str) -> int:
    return abs(bisect_left(open_days, a) - bisect_left(open_days, b))


def cross_check_actions(
    conn: sqlite3.Connection,
    symbol: str,
    market: str,
    plateaus: list[Plateau],
    max_lag_td: int = MAX_CA_LAG_TD,
) -> tuple[list[str], list[str]]:
    """平台切换日与 corporate_actions 除权日交叉印证（偏差 > max_lag 交易日记警告，不阻断）。

    返回 (warnings, notes)。
    """
    warnings: list[str] = []
    notes: list[str] = []
    switch_dates = [p.start for p in plateaus[1:]]
    ca_rows = conn.execute(
        "SELECT ex_date, action_type FROM corporate_actions WHERE symbol = ? "
        "ORDER BY ex_date", (symbol,),
    ).fetchall()
    if not switch_dates:
        notes.append("无平台段切换（无除权），交叉印证跳过")
        return warnings, notes
    if not ca_rows:
        notes.append(
            f"corporate_actions 无 {symbol} 记录，{len(switch_dates)} 个平台切换日未交叉印证")
        return warnings, notes
    ex_dates = [r["ex_date"] for r in ca_rows]
    calendar = load_calendar(conn, market)
    open_days = sorted(d for d, r in calendar.items() if r["is_open"]) if calendar else []
    for sw in switch_dates:
        if open_days:
            nearest = min(ex_dates, key=lambda e: _trading_day_distance(open_days, sw, e))
            dist = _trading_day_distance(open_days, sw, nearest)
            unit = "交易日"
        else:  # 日历缺失降级为自然日
            nearest = min(ex_dates, key=lambda e: abs(
                (datetime.fromisoformat(e) - datetime.fromisoformat(sw)).days))
            dist = abs((datetime.fromisoformat(nearest) - datetime.fromisoformat(sw)).days)
            unit = "自然日（日历缺失降级）"
        if dist > max_lag_td:
            warnings.append(
                f"平台切换日 {sw} 与最近 corporate_actions 除权日 {nearest} "
                f"偏差 {dist} {unit} > {max_lag_td}，请人工核对（不阻断）")
    matched = sum(1 for sw in switch_dates if any(
        (open_days and _trading_day_distance(open_days, sw, e) <= max_lag_td) for e in ex_dates))
    notes.append(f"交叉印证：{matched}/{len(switch_dates)} 个平台切换日与 "
                 f"corporate_actions 除权日对齐（容差 {max_lag_td} 交易日）")
    return warnings, notes


# ---------------------------------------------------------------- 主流程

def _load_split_events(conn: sqlite3.Connection, symbol: str) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT ex_date, split_ratio, action_type FROM corporate_actions "
        "WHERE symbol = ? AND split_ratio IS NOT NULL ORDER BY ex_date", (symbol,),
    ).fetchall()
    events = []
    for r in rows:
        if r["action_type"] in _SPLIT_TYPES:
            events.append((r["ex_date"], float(r["split_ratio"])))
    return events


def _record_run(conn: sqlite3.Connection, run_id: str, stage: str, *,
                status: str, data_cutoff: str | None = None,
                started_at: str, error: str | None = None) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            status, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, stage, utc_now(), data_cutoff, status, error, started_at, utc_now()),
    )


def apply_adjustment(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    forward_csv: str | Path | None = None,
    forward_closes: dict[str, float] | None = None,
    run_id: str | None = None,
    tol: float = TOL_REL,
) -> AdjustResult:
    """全量重建该股复权因子并同事务重建周线（调用方负责事务/提交）。

    不复权来源：库内 daily_bars.close_raw（已校验的规范化事实）；
    前复权来源：forward_csv（新采 adjust=forward 文件）或直接给 forward_closes。
    """
    started_at = utc_now()
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"adjust_{symbol}_{now_compact}"
    res = AdjustResult(symbol=symbol, run_id=run_id)

    rows = conn.execute(
        "SELECT trade_date, close_raw, market FROM daily_bars "
        "WHERE symbol = ? ORDER BY trade_date", (symbol,),
    ).fetchall()
    if not rows:
        raise ValueError(f"{symbol} 无 daily_bars，先入库不复权行情")
    raw_closes = {r["trade_date"]: r["close_raw"] for r in rows}
    market = rows[0]["market"]
    res.origin_date = rows[0]["trade_date"]

    if forward_closes is None:
        if forward_csv is None:
            raise ValueError("需要 forward_csv 或 forward_closes 之一")
        forward_closes = load_forward_closes(forward_csv)
    f = compute_source_factor(raw_closes, forward_closes)
    missing = sorted(set(raw_closes) - set(f))
    if missing:
        res.notes.append(
            f"{len(missing)} 个交易日无前复权数据（{missing[0]}~{missing[-1]}），保留旧因子")

    res.plateaus = detect_plateaus(f, tol)
    res.switch_dates = [p.start for p in res.plateaus[1:]]
    series = plateau_series(f, res.plateaus)
    internal = normalize_factors(series, res.origin_date)
    f_origin = series[res.origin_date]

    split_events = _load_split_events(conn, symbol)
    share_factors, todos = compute_share_factors(list(raw_closes), split_events)
    res.notes.extend(todos)

    warnings, notes = cross_check_actions(conn, symbol, market, res.plateaus)
    res.warnings.extend(warnings)
    res.notes.extend(notes)

    # 因子列更新（派生数据重算，§2.2 第 3 类；OHLC 原始值不动）
    now = utc_now()
    for d in raw_closes:
        if d not in internal:
            res.bars_kept += 1
            continue
        conn.execute(
            "UPDATE daily_bars SET price_adj_factor = ?, share_factor = ?, "
            "updated_at = ? WHERE symbol = ? AND trade_date = ?",
            (round(internal[d], 6), round(share_factors[d], 6), now, symbol, d),
        )
        res.bars_updated += 1

    # 因子版本记录（算法、origin、来源、平台段明细）
    notes_json = {
        "source_factor_at_origin": f_origin,
        "tolerance_rel": tol,
        "plateaus": [{"start": p.start, "end": p.end, "factor": p.factor}
                     for p in res.plateaus],
        "switch_dates": res.switch_dates,
        "warnings": res.warnings,
        "share_todos": todos,
    }
    if forward_csv is not None:
        notes_json["forward_csv"] = str(forward_csv)
        notes_json["forward_csv_sha256"] = sha256_file(forward_csv)
    cur = conn.execute(
        """
        INSERT INTO adjustment_factor_versions (symbol, factor_origin_date, direction,
            algorithm, source, run_id, notes, created_at)
        VALUES (?, ?, 'forward_cumulative', ?, ?, ?, ?, ?)
        """,
        (symbol, res.origin_date, ALGORITHM,
         "stock_finance_data forward/none", run_id,
         json.dumps(notes_json, ensure_ascii=False), now),
    )
    res.version_id = cur.lastrowid

    # 周线同事务重建（§2.2：因子与周线同一原子提交）
    res.weekly = rebuild_weekly(conn, symbol, run_id=run_id)

    _record_run(conn, run_id, "adjust",
                status="degraded" if res.warnings else "success",
                data_cutoff=rows[-1]["trade_date"], started_at=started_at)
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.adjust")
    parser.add_argument("symbol")
    parser.add_argument("--forward-csv", required=True,
                        help="新采 adjust=forward 前复权 CSV（全量重建用 3 年，"
                             "--check-only 时用重叠窗口）")
    parser.add_argument("--check-only", action="store_true",
                        help="只做重叠窗口因子变化检测，不写库")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.check_only:
            res = check_factor_change(conn, args.symbol, args.forward_csv)
            print(res)
            if res.changed:
                print("→ 应对该股全量重采 forward 并运行 adjust 重建因子、周线、指标与信号（§3.3）")
            return 0 if not res.changed else 3
        with conn:  # 因子更新 + 版本 + 周线重建同一事务
            res = apply_adjustment(conn, args.symbol, forward_csv=args.forward_csv)
        print(res)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
