"""三因子 Top-N 周频轮动回测 CLI（Phase 3，计划 §9–§12 最小实现）。

用法：
    uv run python -m scripts.backtest.run_factor
    uv run python -m scripts.backtest.run_factor --top-n 5 --symbols 000001.SZ,...

流程（因子与交易分离）：只读 market.db → 股票池 → 引擎外预计算三因子横截面
评分 → 注入策略 → akquant 回测 → 打印指标 / 因子覆盖率 / 基准对比；不落库。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from scripts.backtest import db as bdb
from scripts.backtest.data import load_symbol
from scripts.backtest.factors import (
    FactorParams,
    build_factor_table,
    build_score_map,
)
from scripts.backtest.run import _METRIC_KEYS
from scripts.backtest.run_multi import benchmark_period_return, prepare_data
from scripts.backtest.strategies.factor_rotation import make_factor_strategy
from scripts.backtest.universe import load_universe

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "backtest.yaml"


def load_factor_params(config_path: str | Path | None = None) -> FactorParams:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    f = doc.get("factors") or {}
    return FactorParams(
        momentum_window=int(f.get("momentum_window", 20)),
        volatility_window=int(f.get("volatility_window", 20)),
        liquidity_window=int(f.get("liquidity_window", 20)),
        weights=dict(f.get("weights") or {"momentum": 0.5, "volatility": -0.3,
                                          "liquidity": 0.2}),
        winsorize_pct=float(f.get("winsorize_pct", 0.05)),
        min_names=int(f.get("min_names", 3)),
    )


def run_factor(symbols: list[str] | None = None, *, top_n: int | None = None,
               start: str | None = None, end: str | None = None,
               cfg: dict | None = None, factor_params: FactorParams | None = None,
               config_path: str | Path | None = None,
               db_path=None) -> tuple[dict, object]:
    """执行三因子轮动回测，返回 (结果字典, akquant BacktestResult)。"""
    from scripts.backtest.run import load_config, build_kwargs

    cfg = cfg if cfg is not None else load_config()
    p = factor_params if factor_params is not None else load_factor_params(config_path)
    max_window = max(p.momentum_window, p.volatility_window, p.liquidity_window)
    kwargs = build_kwargs(cfg)
    with bdb.connect(db_path) as conn:
        pool = load_universe(conn, symbols)
        df, valid, skipped = prepare_data(
            conn, pool, start=start, end=end,
            min_bars=max_window + 5, include_amount=True)
        per_symbol: dict[str, object] = {}
        for sym in valid:
            per_symbol[sym] = df[df["symbol"] == sym].reset_index(drop=True)
        bench_ret = benchmark_period_return(
            conn, "000300.SH", str(df["date"].min()), str(df["date"].max()))

    tidy, stats = build_factor_table(per_symbol, p)
    score_map = build_score_map(tidy)

    from akquant import run_backtest
    strategy_cls = make_factor_strategy(valid, score_map)
    strategy = strategy_cls(**({"top_n": top_n} if top_n is not None else {}))
    result = run_backtest(data=df, strategy=strategy, symbols=valid, **kwargs)

    metrics = result.metrics
    out = {k: getattr(metrics, k) for k in _METRIC_KEYS if hasattr(metrics, k)}
    out["_universe_valid"] = valid
    out["_skipped"] = skipped
    out["_n_bars"] = int(len(df))
    out["_start"] = str(df["date"].iloc[0])
    out["_end"] = str(df["date"].iloc[-1])
    orders = result.orders_df
    trades = result.trades_df
    positions = result.positions_df
    out["_n_orders"] = int(len(orders)) if orders is not None else 0
    out["_n_trades"] = int(len(trades)) if trades is not None else 0
    out["_n_rejected"] = (
        int((orders["status"] == "rejected").sum())
        if orders is not None and "status" in orders.columns else 0)
    traded = set(trades["symbol"]) if trades is not None and len(trades) else set()
    out["_traded_symbols"] = sorted(traded)
    scored_syms = {s for mp in score_map.values() for s in mp}
    out["_scored_symbols"] = sorted(scored_syms)
    # 参与交易的标的不应超出有分数的集合（取分纪律的外部佐证）
    out["_trade_leakage"] = sorted(traded - scored_syms)
    out["_factor_stats"] = {
        k: v for k, v in stats.items() if k != "per_symbol_score_days"}
    out["_factor_coverage_pct"] = {
        s: round(100.0 * n / max(stats["total_trade_days"], 1), 1)
        for s, n in stats["per_symbol_score_days"].items()}
    out["_benchmark_000300_pct"] = bench_ret
    return out, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.backtest.run_factor")
    parser.add_argument("--symbols", default=None,
                        help="逗号分隔股票池覆盖；默认 watchlist active 全部")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    out, result = run_factor(
        symbols, top_n=args.top_n, start=args.start, end=args.end,
        config_path=args.config or None,
        cfg=load_config(args.config) if args.config else None)
    if args.json:
        print(json.dumps({k: v for k, v in out.items() if not hasattr(v, "shape")},
                         ensure_ascii=False, indent=2))
        return 0

    print("== 三因子 Top-N 周频等权轮动 ==")
    print(f"股票池: {len(out['_universe_valid'])} 只有效"
          f"（跳过 {len(out['_skipped'])}：{'; '.join(out['_skipped']) or '无'}）")
    print(f"区间: {out['_start']} ~ {out['_end']}（{out['_n_bars']} 行 bar）")
    st = out["_factor_stats"]
    print(f"因子评分: {out['_scored_symbols'] and len(out['_scored_symbols']) or 0} 只"
          f"有分，首日 {st['first_score_date']}，末日 {st['last_score_date']}，"
          f"中性截面计数 {st['neutral_factor_dates']}")
    cov = out["_factor_coverage_pct"]
    low_cov = {s: pc for s, pc in cov.items() if pc < 80}
    print(f"覆盖率<80%: {low_cov or '无'}")
    for k in _METRIC_KEYS:
        if k in out:
            print(f"  {k}: {out[k]}")
    print(f"  交易笔数: {out['_n_trades']}（orders {out['_n_orders']}，"
          f"拒单 {out['_n_rejected']}）")
    if out["_trade_leakage"]:
        print(f"  ⚠️ 无分标的参与交易: {out['_trade_leakage']}")
    bench = out["_benchmark_000300_pct"]
    print(f"  沪深300 同区间: "
          + (f"{bench:.2f}% vs 策略 {out['total_return_pct']:.2f}%"
             if bench is not None else "无同区间指数数据，跳过对比"))
    positions = result.positions_df
    if positions is not None and len(positions):
        last_day = positions["date"].max()
        tail = positions[positions["date"] == last_day]
        cols = [c for c in ("symbol", "quantity", "market_value")
                if c in tail.columns]
        print(f"\n== 末日持仓快照（{last_day}，{len(tail)} 个标的）==")
        print(tail[cols].to_string(index=False) if cols
              else tail.head().to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
