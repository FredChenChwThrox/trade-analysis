"""回测 CLI（Phase 1 单股验证）。

用法：
    uv run python -m scripts.backtest.run --symbol 000001.SZ
    uv run python -m scripts.backtest.run --symbol 000001.SZ --start 2024-01-01

行为：只读 market.db → akquant 回测 → stdout 打印指标与交易明细摘要；不落库。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from akquant import run_backtest

from scripts.backtest import data as bdata
from scripts.backtest import db as bdb
from scripts.backtest.strategies.dual_ma import DualMAStrategy

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "backtest.yaml"

_METRIC_KEYS = [
    "total_return_pct", "annualized_return", "sharpe_ratio", "max_drawdown_pct",
    "volatility", "win_rate", "profit_factor", "trade_count",
]


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else Path(DEFAULT_CONFIG)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {k: doc[k] for k in (
        "initial_cash", "lot_size", "t_plus_one", "commission_rate",
        "stamp_tax_rate", "transfer_fee_rate", "min_commission",
        "slippage", "timezone",
    ) if k in doc}


def build_kwargs(cfg: dict) -> dict:
    """从 YAML 组装 run_backtest 参数（缺省不传，用 akquant 默认）。

    slippage 裸数字已废弃，显式转 percent policy。
    """
    kwargs = {}
    for k in ("initial_cash", "lot_size", "t_plus_one", "commission_rate",
              "stamp_tax_rate", "transfer_fee_rate", "min_commission",
              "timezone"):
        if k in cfg:
            kwargs[k] = cfg[k]
    if "slippage" in cfg:
        kwargs["slippage"] = {"type": "percent", "value": cfg["slippage"]}
    return kwargs


def run(symbol: str, cfg: dict | None = None, *, start: str | None = None,
        end: str | None = None, config_path: str | Path | None = None,
        metrics_keys: list[str] | None = None, db_path: str | Path | None = None
        ) -> tuple[dict, "object"]:
    """执行单股双均线回测，返回 (指标字典, akquant BacktestResult)。

    result 供 main 做 Phase 1 验证打印（trades/orders 明细），不落库。
    """
    cfg = cfg if cfg is not None else load_config(config_path)
    kwargs = build_kwargs(cfg)
    with bdb.connect(db_path) as conn:
        df = bdata.load_symbol(conn, symbol, start=start, end=end)

    result = run_backtest(
        data=df,
        strategy=DualMAStrategy,
        symbols=symbol,
        **kwargs,
    )
    metrics = result.metrics
    keys = metrics_keys or _METRIC_KEYS
    out = {k: getattr(metrics, k) for k in keys if hasattr(metrics, k)}
    out["_symbol"] = symbol
    out["_n_bars"] = int(len(df))
    out["_start"] = str(df["date"].iloc[0])
    out["_end"] = str(df["date"].iloc[-1])
    out["_n_trades"] = int(len(result.trades_df)) if result.trades_df is not None else 0
    return out, result


def _print_verification(result) -> None:
    """Phase 1 验证输出：交易方向/T+1/费用可复算性。"""
    trades = result.trades_df
    orders = result.orders_df
    print("\n== 验证摘要 ==")
    print(f"orders 行数: {len(orders) if orders is not None else 0}")
    print(f"trades 行数: {len(trades) if trades is not None else 0}")
    if trades is not None and len(trades):
        sides = trades["side"].tolist() if "side" in trades.columns else None
        print(f"trades side 分布: {sides}")
        print(f"trades 列: {list(trades.columns)}")
    if orders is not None and len(orders):
        print(f"orders 列: {list(orders.columns)}")
        rej_mask = orders["reject_reason"].fillna("").astype(str).str.len() > 5
        rej = orders[rej_mask]
        print(f"拒单数: {len(rej)}（T+1 下卖出被拒属预期，详见原因）")
        for _, r in rej.head(5).iterrows():
            print(f"  [{r['created_at_iso']}] {r['side']} qty={r['quantity']} "
                  f"-> {r['reject_reason'][:120]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.backtest.run")
    parser.add_argument("--symbol", default="000001.SZ", help="库内 symbol（默认 000001.SZ）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="回测 YAML")
    parser.add_argument("--start", default=None, help="可选起始日 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="可选结束日 YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 指标")
    args = parser.parse_args(argv)

    out, result = run(args.symbol, start=args.start, end=args.end,
                      config_path=args.config)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"== {out['_symbol']} 双均线回测 ==")
    print(f"区间: {out['_start']} ~ {out['_end']}（{out['_n_bars']} 个交易日）")
    for k in _METRIC_KEYS:
        if k in out:
            print(f"  {k}: {out[k]}")
    print(f"  交易笔数: {out['_n_trades']}")

    _print_verification(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
