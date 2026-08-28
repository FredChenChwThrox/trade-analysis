"""从 data/market.db 加载单股复权行情 → akquant DataFrame。

口径（与系统 §3.3/§4.1 一致，Phase 1 不做 qfq 换算）：
- 调整价 = raw × price_adj_factor（后复权，历史值稳定）
- 调整量 = volume_raw ÷ share_factor
- date 为市场本地日期（YYYY-MM-DD），open/high/low/close/volume 为 float
- 输出列 date/open/high/low/close/volume/symbol，按 (date, symbol) 升序
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from scripts.backtest.db import connect


def load_symbol(conn: sqlite3.Connection, symbol: str,
                start: str | None = None, end: str | None = None,
                include_amount: bool = False) -> pd.DataFrame:
    """读取单股每日复权行情。无数据时抛 ValueError（§2.5 不猜）。

    include_amount=True 时附带 amount_raw 列（流动性因子用；成交额不做
    股份调整，§4.1）。库内 amount 存在已知缺口（kimi 源无），调用方自处理。
    """
    sql = """
        SELECT trade_date, open_raw, high_raw, low_raw, close_raw,
               volume_raw, price_adj_factor, share_factor, amount_raw
        FROM daily_bars
        WHERE symbol = ?
    """
    params: list = [symbol]
    if start:
        sql += " AND trade_date >= ?"
        params.append(start)
    if end:
        sql += " AND trade_date <= ?"
        params.append(end)
    sql += " ORDER BY trade_date"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        raise ValueError(f"daily_bars 无 {symbol} 数据（start={start} end={end}）")

    df = pd.DataFrame([dict(r) for r in rows])
    df["open"] = df["open_raw"] * df["price_adj_factor"]
    df["high"] = df["high_raw"] * df["price_adj_factor"]
    df["low"] = df["low_raw"] * df["price_adj_factor"]
    df["close"] = df["close_raw"] * df["price_adj_factor"]
    df["volume"] = df["volume_raw"] / df["share_factor"]
    df["symbol"] = symbol
    df = df.rename(columns={"trade_date": "date"})
    cols = ["date", "open", "high", "low", "close", "volume", "symbol"]
    if include_amount:
        cols.append("amount_raw")
    return df[cols].copy()


def load_symbol_df(symbol: str, start: str | None = None,
                   end: str | None = None) -> pd.DataFrame:
    """便捷入口：自动开只读连接并加载单股。"""
    with connect() as conn:
        return load_symbol(conn, symbol, start=start, end=end)
