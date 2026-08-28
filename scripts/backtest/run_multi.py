"""多股 Top-N 周频轮动回测 CLI（Phase 2）。

用法：
    uv run python -m scripts.backtest.run_multi
    uv run python -m scripts.backtest.run_multi --top-n 5 --lookback 20
    uv run python -m scripts.backtest.run_multi --symbols 000001.SZ,600029.SH

行为：只读 market.db → watchlist 股票池（样本不足剔除并明示）→ akquant 多股回测
→ stdout 打印指标 / 交易摘要 / 持仓快照 / 沪深300 同区间对比；不落库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3

import pandas as pd

from akquant import run_backtest

from scripts.backtest import data as bdata
from scripts.backtest import db as bdb
from scripts.backtest.run import build_kwargs, load_config, _METRIC_KEYS
from scripts.backtest.strategies.topn_rotation import make_topn_strategy
from scripts.backtest.universe import load_universe

_MIN_SLACK = 5          # lookback 之外的裕量 bar 数（首周触发前还要有完整 warmup）
_DEFAULT_TOP_N = 5
_DEFAULT_LOOKBACK = 20


def prepare_data(conn: sqlite3.Connection, universe: list[str], *,
                 start: str | None, end: str | None,
                 min_bars: int,
                 include_amount: bool = False) -> tuple[pd.DataFrame, list[str], list[str]]:
    """逐股加载并拼接；样本不足的剔入 skipped 并给出原因。返回 (df, 有效股池, skipped)。"""
    frames: list[pd.DataFrame] = []
    valid: list[str] = []
    skipped: list[str] = []
    for sym in universe:
        try:
            df = bdata.load_symbol(conn, sym, start=start, end=end,
                                   include_amount=include_amount)
        except ValueError as exc:
            skipped.append(f"{sym}: 无数据（{exc}）")
            continue
        if len(df) < min_bars:
            skipped.append(f"{sym}: 样本不足（{len(df)} < {min_bars}）")
            continue
        frames.append(df)
        valid.append(sym)
    if not frames:
        raise ValueError("无任何股票满足最小样本要求，无法回测")
    all_df = pd.concat(frames).sort_values(["date", "symbol"]).reset_index(drop=True)
    return all_df, valid, skipped


def benchmark_period_return(conn: sqlite3.Connection, index_code: str,
                            start: str, end: str) -> float | None:
    """基准指数同区间收益（%）；无数据返回 None 不猜。"""
    rows = conn.execute(
        """
        SELECT trade_date, close FROM index_bars
        WHERE index_code = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (index_code, start, end),
    ).fetchall()
    if len(rows) < 2 or not rows[0]["close"]:
        return None
    return (rows[-1]["close"] / rows[0]["close"] - 1.0) * 100.0


def run_multi(symbols: list[str] | None = None, *, top_n: int | None = None,
              lookback: int | None = None, start: str | None = None,
              end: str | None = None, cfg: dict | None = None,
              db_path=None) -> dict:
    """执行多股轮动回测，返回 (结果字典, akquant BacktestResult)。

    top_n/lookback 经策略构造器传入（akquant 内联参数语义）；未传用类默认。
    """
    cfg = cfg if cfg is not None else load_config()
    kwargs = build_kwargs(cfg)
    with bdb.connect(db_path) as conn:
        pool = load_universe(conn, symbols)
        min_bars = (lookback or _DEFAULT_LOOKBACK) + _MIN_SLACK
        df, valid, skipped = prepare_data(
            conn, pool, start=start, end=end, min_bars=min_bars)
        bench_ret = benchmark_period_return(
            conn, "000300.SH", str(df["date"].min()), str(df["date"].max()))

    strategy_cls = make_topn_strategy(valid)
    if top_n is not None or lookback is not None:
        overrides = {}
        if top_n is not None:
            overrides["top_n"] = top_n
        if lookback is not None:
            overrides["lookback"] = lookback
        strategy = strategy_cls(**overrides)  # 失败即抛错，不静默回退
    else:
        strategy = strategy_cls()
    result = run_backtest(
        data=df, strategy=strategy, symbols=valid, **kwargs)

    metrics = result.metrics
    out = {k: getattr(metrics, k) for k in _METRIC_KEYS if hasattr(metrics, k)}
    out["_universe_valid"] = valid
    out["_skipped"] = skipped
    out["_n_bars"] = int(len(df))
    out["_start"] = str(df["date"].iloc[0])
    out["_end"] = str(df["date"].iloc[-1])
    trades = result.trades_df
    orders = result.orders_df
    positions = result.positions_df
    out["_n_trades"] = int(len(trades)) if trades is not None else 0
    out["_n_orders"] = int(len(orders)) if orders is not None else 0
    out["_n_rejected"] = (
        int((orders["status"] == "rejected").sum())
        if orders is not None and "status" in orders.columns else 0)
    traded = set()
    if trades is not None and len(trades):
        traded = set(trades["symbol"])
    elif positions is not None and len(positions):
        traded = set(positions["symbol"])
    out["_traded_symbols"] = sorted(traded)
    out["_benchmark_000300_pct"] = bench_ret
    return out, result


def _print_result(out: dict, result) -> None:
    print(f"== 多股 Top-N 周频等权轮动 ==")
    print(f"股票池: {len(out['_universe_valid'])} 只有效"
          f"（跳过 {len(out['_skipped'])}：{'; '.join(out['_skipped']) or '无'}）")
    print(f"区间: {out['_start']} ~ {out['_end']}（{out['_n_bars']} 行 bar）")
    for k in _METRIC_KEYS:
        if k in out:
            print(f"  {k}: {out[k]}")
    print(f"  交易笔数: {out['_n_trades']}（orders {out['_n_orders']}，"
          f"拒单 {out['_n_rejected']}——资金余量内的次周自愈，持续偏高则说明缓冲不足）")
    print(f"  参与交易标的: {out['_traded_symbols']}")
    if out["_benchmark_000300_pct"] is not None:
        print(f"  沪深300 同区间: {out['_benchmark_000300_pct']:.2f}%"
              f" vs 策略 {out.get('total_return_pct', float('nan')):.2f}%")
    else:
        print("  沪深300: index_bars 无同区间数据，跳过对比")
    positions = result.positions_df
    if positions is not None and len(positions) and "date" in positions.columns:
        last_day = positions["date"].max()
        tail = positions[positions["date"] == last_day]
        cols = [c for c in ("symbol", "quantity", "market_value") if c in tail.columns]
        print(f"\n== 末日持仓快照（{last_day}，{len(tail)} 个标的）==")
        print(tail[cols].to_string(index=False) if cols else tail.head().to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.backtest.run_multi")
    parser.add_argument("--symbols", default=None,
                        help="逗号分隔股票池覆盖；默认 watchlist active 全部")
    parser.add_argument("--top-n", type=int, default=_DEFAULT_TOP_N)
    parser.add_argument("--lookback", type=int, default=_DEFAULT_LOOKBACK)
    parser.add_argument("--config", default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    cfg = load_config(args.config) if args.config else load_config()
    out, result = run_multi(symbols, top_n=args.top_n, lookback=args.lookback,
                            start=args.start, end=args.end, cfg=cfg)
    if args.json:
        payload = {k: v for k, v in out.items() if not hasattr(v, "shape")}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    _print_result(out, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
