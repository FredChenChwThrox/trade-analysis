"""衰竭信号时序事件回测 CLI（Phase A：排期卡择时层忠实机械化验证）。

用法：
    uv run python -m scripts.backtest.run_event                       # 全 watchlist
    uv run python -m scripts.backtest.run_event --symbols 603605.SH
    uv run python -m scripts.backtest.run_event --min-signals 2 --stop-pct 0.08

语义：逐股独立账户时序回测（每只单独 run_backtest 后聚合），不做横截面
资金分配（那是 Phase B）。入场=最近完成周同锚 active 衰竭 ≥ min_signals；
出场=收盘跌破机械证伪线（decline_start HFQ 止损位）。
"""

from __future__ import annotations

import argparse
import json
from statistics import mean

from akquant import run_backtest

from scripts.backtest import db as bdb
from scripts.backtest.data import load_symbol
from scripts.backtest.event_signals import (
    anchor_for_date,
    latest_week_before,
    load_completed_weeks,
    load_decline_starts,
    load_exhaustion_counts,
)
from scripts.backtest.run import _METRIC_KEYS, build_kwargs, load_config
from scripts.backtest.strategies.exhaustion_timing import make_exhaustion_strategy


def prepare_symbol_inputs(conn, symbol: str, *, stop_pct: float,
                          min_signals: int) -> tuple[dict, list, list] | None:
    """读取事件表；无完成周或全库零触发事件返回 None（跳过并明示）。"""
    counts = load_exhaustion_counts(conn, symbol)   # {week: (anchor_id, n)}
    weeks = load_completed_weeks(conn, symbol)
    anchors = load_decline_starts(conn, symbol, stop_pct)
    if not weeks:
        return None
    # 只保留已完成周历上的计数，且预筛出足额触发的周
    events = {w: (aid, n) for w, (aid, n) in counts.items()
              if w in set(weeks) and n >= min_signals}
    anchor_rows = [(a.trade_date, a.stop_adj, a.adj_price) for a in anchors]
    return {"events": events, "weeks": weeks, "anchors": anchor_rows}


def run_event_one(symbol: str, cfg: dict, *, stop_pct: float,
                  min_signals: int, entry_discount_pct: float = 0.0,
                  db_path=None) -> dict:
    """单股事件回测，返回指标+事件摘要。"""
    kwargs = build_kwargs(cfg)
    with bdb.connect(db_path) as conn:
        df = load_symbol(conn, symbol)   # 无行情直接抛，由上层归 skipped
        inputs = prepare_symbol_inputs(
            conn, symbol, stop_pct=stop_pct, min_signals=min_signals)
        if inputs is None:
            return {"_symbol": symbol, "_skipped": "无完成周线或零事件"}

    strategy_cls = make_exhaustion_strategy(
        inputs["events"], inputs["weeks"], inputs["anchors"],
        entry_discount_pct=entry_discount_pct)
    result = run_backtest(
        data=df, strategy=strategy_cls(), symbols=symbol, **kwargs)

    metrics = result.metrics
    out = {k: getattr(metrics, k) for k in _METRIC_KEYS if hasattr(metrics, k)}
    trades = result.trades_df
    out["_symbol"] = symbol
    out["_n_entry_events_weeks"] = len(inputs["events"])
    out["_trades"] = int(len(trades)) if trades is not None else 0
    out["_skipped"] = None
    if trades is not None and len(trades):
        out["_avg_return_pct"] = float(trades["return_pct"].astype(float).mean())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.backtest.run_event")
    parser.add_argument("--symbols", default=None,
                        help="逗号分隔；默认 watchlist active 全部")
    parser.add_argument("--min-signals", type=int, default=2,
                        help="入场所需同锚 active 信号数（默认 2，卡片口径）")
    parser.add_argument("--stop-pct", type=float, default=0.08,
                        help="机械证伪线：decline_start 下方百分比（默认 8%%）")
    parser.add_argument("--entry-discount-pct", type=float, default=0.0,
                        help="入场折扣门：close ≤ 最新锚 adj×(1-x)，0 关闭；"
                             "近似卡片价区半条件（默认纯信号版）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from scripts.backtest.universe import load_universe
    cfg = load_config(args.config) if args.config else load_config()

    with bdb.connect() as conn:
        pool = (sorted({s.strip() for s in args.symbols.split(",") if s.strip()})
                if args.symbols else load_universe(conn))

    results = []
    for sym in pool:
        try:
            results.append(run_event_one(
                sym, cfg, stop_pct=args.stop_pct, min_signals=args.min_signals,
                entry_discount_pct=args.entry_discount_pct))
        except Exception as exc:  # noqa: BLE001  单股失败不拖垮汇总（逐股原子语义）
            results.append({"_symbol": sym, "_skipped": f"{type(exc).__name__}: {exc}"})
        print(f"[{sym}] {'SKIP ' + results[-1]['_skipped'] if results[-1]['_skipped'] else 'ok'}")

    ok = [r for r in results if not r["_skipped"]]
    print(f"\n== Phase A 汇总：{len(ok)}/{len(results)} 只参与 ==")
    for r in ok:
        print(f"  {r['_symbol']:10} 入场周 {r['_n_entry_events_weeks']:3} | "
              f"交易 {r['_trades']:2} 笔 | "
              f"总收益 {r.get('total_return_pct', float('nan')):7.2f}% | "
              f"胜率 {r.get('win_rate', float('nan')):5.1f}% | "
              f"最大回撤 {r.get('max_drawdown_pct', float('nan')):6.2f}%"
              + (f" | 单笔均收 {r['_avg_return_pct']:.2f}%" if "_avg_return_pct" in r else ""))

    traded_ok = [r for r in ok if r["_trades"] > 0]
    if traded_ok:
        rets = [r["total_return_pct"] for r in traded_ok]
        wins = [r["win_rate"] for r in traded_ok]
        print(f"\n池级（有交易的 {len(traded_ok)} 只）："
              f"总收益中位 {sorted(rets)[len(rets)//2]:.2f}% / 均 {mean(rets):.2f}%；"
              f"胜率中位 {sorted(wins)[len(wins)//2]:.1f}%")
        positive = sum(1 for x in rets if x > 0)
        print(f"正收益家数: {positive}/{len(traded_ok)}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
