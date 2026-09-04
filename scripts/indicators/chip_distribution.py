"""自算筹码分布（换手率衰减模型）——观察项，不进信号链（§2.5）。

设计：docs/superpowers/specs/2026-09-04-chip-distribution-design.md（v2 评审修订版）。

模型（复权价格域，§2.1–2.2）：

    w_d(p) = (1 − k_d) × w_{d−1}(p) + k_d × B_d(p)
    k_d    = min(A × turnover_d, k_cap)；停牌 / turnover 缺失 → k_d = 0
    B_d    = 三角核（peak = close_d，支撑 [low_d, high_d]；一字板退化为点分布）

- 初始化：窗口首日全部筹码均匀铺在当日 [low, high]（burn_in 打标处理初始化偏差，§3）
- 复权域直算、输出 ÷ 当日 price_adj_factor 折回不复权（§2.3）；
  现金分红在前复权口径下平移历史成本（不还原真实股东成本，偏差声明见设计 §2.3/§7）
- 离散化口径：价格网格 bin 质量集中于 bin 中心，winner_ratio / 分位数由中心累积
  权重线性插值（bin 级积分为三角 CDF 闭式差分，唯插值带 O(bin 宽) 误差）

用法：

    uv run python -m scripts.indicators.chip_distribution <symbol> [--as-of D]
    uv run python -m scripts.indicators.chip_distribution --all

幂等：DELETE + 重插 + pipeline_runs 同事务（§6 派生表惯例）；--all 单一全局 run_id。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from scripts.indicators.compute import load_params
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now

SOURCE = "self_computed"
_ROOT = Path(__file__).resolve().parents[2]

_PEAK_ABBR = {"close": "close", "vwap": "vwap"}
_SHAPE_ABBR = {"triangular": "tri", "uniform": "unif"}

# 默认参数（config/indicators.yaml defaults.chip 缺失时兜底；⚠️ 与 yaml 注释同步）
_DEFAULTS = {"decay_factor": 0.7, "turnover_cap": 0.8, "peak_mode": "close",
             "dist_shape": "triangular", "n_bins": 2000, "burn_in_days": 90,
             "price_pad": 0.1}


def rule_version(params: dict) -> str:
    """核形状/峰值编码进版本串（评审 #5：改核不改参也可分辨）。"""
    return (f"chip_v1_{_PEAK_ABBR[params['peak_mode']]}"
            f"_{_SHAPE_ABBR[params['dist_shape']]}")


@dataclass
class ChipResult:
    symbol: str = ""
    run_id: str = ""
    config_hash: str = ""
    rule_version: str = ""
    rows: int = 0
    burn_in_rows: int = 0
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"{self.symbol} run_id={self.run_id} rule_version={self.rule_version}",
            f"config_hash={self.config_hash[:12]}…",
            f"chip_distribution {self.rows} 行（burn_in {self.burn_in_rows}）",
        ]
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------- 纯函数（golden 可测）


def _tri_cdf_at(x: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    """三角分布 CDF（支撑 [a,b]、峰值 c）在 x 处的闭式值；c==a / c==b 边界单独处理。

    支撑外严格 0/1（x<a 为 0、x≥b 为 1）——用布尔索引分区域赋值，禁止
    np.where 顺序覆盖（x<a 区域会被 val_left=(x-a)^2 误抬，曾致 kernel 和为负）。
    """
    xs = np.asarray(x, dtype=float)
    out = np.zeros_like(xs)
    if b <= a:  # 退化：调用方走点分布路径
        return np.where(xs >= b, 1.0, 0.0)
    left = (xs > a) & (xs <= c)
    right = (xs > c) & (xs < b)
    if c > a:  # 峰左侧：((x-a)^2)/((b-a)(c-a))
        out[left] = (xs[left] - a) ** 2 / ((b - a) * (c - a))
    # c == a：左侧无质量（右三角）
    if b > c:  # 峰右侧：1-((b-x)^2)/((b-a)(b-c))
        out[right] = 1.0 - (b - xs[right]) ** 2 / ((b - a) * (b - c))
    # c == b：右侧无质量（左三角）
    out[xs >= b] = 1.0
    return out


def _kernel_mass(a: float, b: float, c: float, edges: np.ndarray,
                 peak_mode: str, shape: str, amount: float | None,
                 volume: float | None) -> np.ndarray:
    """当日新增筹码核 B_d 的逐 bin 质量（CDF 闭式差分 = 每格精确积分）。"""
    n = len(edges) - 1
    if shape == "uniform":
        cdf = np.clip((edges - a) / (b - a), 0.0, 1.0)
        return np.diff(cdf)
    if peak_mode == "vwap" and amount and volume:
        c = float(np.clip(amount / volume, a, b))
    else:  # close 峰（默认）；amount 缺失时 vwap 兜底回 close（§2.5 不猜）
        c = float(np.clip(c, a, b))
    if a >= b:  # 一字板：点分布
        mass = np.zeros(n)
        idx = int(np.clip(np.searchsorted(edges, a) - 1, 0, n - 1))
        mass[idx] = 1.0
        return mass
    return np.diff(_tri_cdf_at(edges, a, b, c))


def compute_chip_series(days: list[dict], params: dict) -> list[dict]:
    """逐日重放筹码分布（纯函数，golden 可测——同 right_side.evaluate_segment 模式）。

    days 元素字段：trade_date, open, high, low, close, volume, amount（均不复权原值）、
    turnover（小数或 None）、factor（price_adj_factor）、trading_status。
    返回逐日 dict：trade_date / winner_ratio / avg_cost_adj / cost_5_adj / cost_95_adj /
    concentration_90 / estimation_status / turnover_used / amount_used / avg_cost /
    cost_5 / cost_95（后四项已折回不复权）。
    """
    a_coef = float(params["decay_factor"])
    k_cap = float(params["turnover_cap"])
    n_bins = int(params["n_bins"])
    burn_in = int(params["burn_in_days"])
    pad = float(params["price_pad"])
    peak_mode = params.get("peak_mode", "close")
    shape = params.get("dist_shape", "triangular")
    if not days:
        return []

    lo_adj = min(d["low"] * d["factor"] for d in days) * (1.0 - pad)
    hi_adj = max(d["high"] * d["factor"] for d in days) * (1.0 + pad)
    edges = np.linspace(lo_adj, hi_adj, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0

    out: list[dict] = []
    w = None
    for i, d in enumerate(days):
        f = d["factor"] or 1.0
        a = d["low"] * f
        b = d["high"] * f
        c = d["close"] * f
        amount = d.get("amount")
        volume = d.get("volume")
        turnover = d.get("turnover")
        suspended = (d.get("trading_status") == "suspended"
                     or not volume)
        if w is None:
            # 初始化：均匀铺满首日 [low, high]（§2.1）
            mass = _kernel_mass(a, b, (a + b) / 2.0, edges, "vwap", "uniform",
                                None, None)
            w = mass / mass.sum()
        elif not suspended and turnover is not None:
            k = min(a_coef * float(turnover), k_cap)
            w = (1.0 - k) * w + k * _kernel_mass(
                a, b, c, edges, peak_mode, shape, amount, volume)
            w = w / w.sum()
        # 停牌/换手缺失：不衰减不新增，分布原样延续（§2.5）

        total = float(w.sum())
        cum = np.cumsum(w)
        winner = float(np.interp(c, centers, cum))
        avg_adj = float(np.dot(centers, w)) / total
        c5 = float(np.interp(0.05 * total, cum, centers))
        c95 = float(np.interp(0.95 * total, cum, centers))
        conc = (c95 - c5) / (c95 + c5) if (c95 + c5) > 0 else None
        status = "burn_in" if i < burn_in else "mature"
        out.append({
            "trade_date": d["trade_date"], "winner_ratio": winner,
            "avg_cost_adj": avg_adj, "cost_5_adj": c5, "cost_95_adj": c95,
            "concentration_90": conc, "estimation_status": status,
            # turnover_used 语义 = 实际参与衰减的输入（停牌日未用 → None）
            "turnover_used": (float(turnover)
                              if turnover is not None and not suspended else None),
            "amount_used": float(amount) if amount is not None else None,
            # 折回不复权（§2.3）：÷ 当日 factor
            "avg_cost": avg_adj / f, "cost_5": c5 / f, "cost_95": c95 / f,
        })
    return out


# ---------------------------------------------------------------- 入库


def _load_days(conn: sqlite3.Connection, symbol: str, as_of: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT trade_date, open_raw, high_raw, low_raw, close_raw,
               volume_raw, amount_raw, turnover, price_adj_factor, trading_status
        FROM daily_bars WHERE symbol = ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (symbol, as_of),
    ).fetchall()
    days = []
    for r in rows:
        if r["close_raw"] is None or r["high_raw"] is None or r["low_raw"] is None:
            continue  # OHLC 残缺行不进模型（§2.5 不猜）
        days.append({
            "trade_date": r["trade_date"],
            "open": r["open_raw"], "high": r["high_raw"], "low": r["low_raw"],
            "close": r["close_raw"], "volume": r["volume_raw"],
            "amount": r["amount_raw"], "turnover": r["turnover"],
            "factor": r["price_adj_factor"] or 1.0,
            "trading_status": r["trading_status"],
        })
    return days


def recompute_chip(conn: sqlite3.Connection, symbol: str, params: dict,
                   config_hash: str, run_id: str, as_of: str | None = None
                   ) -> ChipResult:
    """单股重算（调用方负责事务）。DELETE + 重插 + pipeline_runs 同事务由调用方包。"""
    res = ChipResult(symbol=symbol, run_id=run_id, config_hash=config_hash,
                     rule_version=rule_version(params))
    if as_of is None:
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars WHERE symbol = ?",
            (symbol,)).fetchone()
        as_of = row["d"] if row and row["d"] else None
    if as_of is None:
        res.notes.append("无 daily_bars，跳过")
        return res
    days = _load_days(conn, symbol, as_of)
    if not days:
        res.notes.append("无有效 OHLC 行，跳过")
        return res
    series = compute_chip_series(days, params)
    now = utc_now()
    conn.execute("DELETE FROM chip_distribution WHERE symbol = ?", (symbol,))
    conn.executemany(
        """
        INSERT INTO chip_distribution (symbol, trade_date, winner_ratio, avg_cost,
            cost_5, cost_95, concentration_90, estimation_status, turnover_used,
            amount_used, source, params_json, run_id, rule_version, config_hash,
            computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(symbol, s["trade_date"], s["winner_ratio"], s["avg_cost"],
          s["cost_5"], s["cost_95"], s["concentration_90"],
          s["estimation_status"], s["turnover_used"], s["amount_used"],
          SOURCE, json.dumps(params, sort_keys=True, ensure_ascii=False),
          run_id, res.rule_version, config_hash, now) for s in series],
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, app_version,
            status, error, started_at, finished_at)
        VALUES (?, 'chip_distribution', ?, ?, NULL, ?, ?, ?, 'success', NULL, ?, ?)
        """,
        (run_id, utc_now(), as_of, config_hash, res.rule_version,
         f"numpy {np.__version__}", now, now),
    )
    res.rows = len(series)
    res.burn_in_rows = sum(1 for s in series if s["estimation_status"] == "burn_in")
    return res


def _chip_params(defaults: dict) -> dict:
    chip = dict(_DEFAULTS)
    chip.update(defaults.get("chip") or {})
    return chip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.indicators.chip_distribution")
    parser.add_argument("symbol", nargs="?", default=None)
    parser.add_argument("--all", action="store_true", help="全池重算（单一 run_id）")
    parser.add_argument("--as-of", default=None, help="截止日（默认库内最新）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    if not args.all and not args.symbol:
        parser.error("需要 <symbol> 或 --all")

    defaults, config_hash = load_params()
    params = _chip_params(defaults)
    from datetime import datetime, timezone
    run_id = ("chip_"
              + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    conn = connect(args.db)
    try:
        if args.all:
            symbols = [r[0] for r in conn.execute(
                "SELECT symbol FROM watchlist WHERE active = 1 ORDER BY symbol")]
        else:
            symbols = [args.symbol]
        ok = failed = 0
        for sym in symbols:
            try:
                with conn:  # DELETE + 重插 + run 记录同一事务（§4.3）
                    res = recompute_chip(conn, sym, params, config_hash,
                                         run_id, args.as_of)
                ok += 1
                if args.symbol or failed:
                    print(res)
                else:
                    print(f"[chip] {sym}: {res.rows} 行 "
                          f"(burn_in {res.burn_in_rows})")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[chip] {sym}: ERROR {type(exc).__name__}: {exc}")
        print(f"== chip_distribution: {rule_version(params)} "
              f"run_id={run_id} ok={ok} failed={failed}")
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
