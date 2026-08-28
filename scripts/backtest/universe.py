"""股票池加载（scripts/backtest 隔离层，只读）。

Phase 2 迷你股票池：默认 watchlist 表 active=1 的 symbol（库内现状 18 只）；
可显式传列表覆盖。扩容到 ≥100 只需先经 akshare 批量采集入库（后续任务）。
"""

from __future__ import annotations

import sqlite3


def load_universe(conn: sqlite3.Connection, symbols: list[str] | None = None) -> list[str]:
    """返回回测股票池。显式列表优先；否则取 watchlist active=1，按 symbol 排序。

    仅校验非空与去重；行情是否足够由 run_multi 按样本数剔除（明示不静默）。
    """
    if symbols is not None:
        pool = sorted({s.strip() for s in symbols if s.strip()})
        if not pool:
            raise ValueError("股票池为空：显式传入的 --symbols 列表为空")
    else:
        rows = conn.execute(
            "SELECT symbol FROM watchlist WHERE active = 1 ORDER BY symbol"
        ).fetchall()
        pool = [r["symbol"] for r in rows]
    if not pool:
        raise ValueError("股票池为空：watchlist 无 active 股票且未显式传 --symbols")
    return pool
