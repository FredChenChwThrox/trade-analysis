"""只读 SQLite 连接（scripts/backtest 自带，不依赖 scripts.pipeline.db）。

隔离说明：监测管线写库，回测只读；这里不复用 scripts/pipeline/db.py，
避免回测代码与管线事务/种子逻辑耦合。库文件路径默认 data/market.db，
可经 BACKTEST_DB 环境变量覆盖（测试用临时库）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "market.db"


def _default_db() -> Path:
    return Path(os.environ.get("BACKTEST_DB", DEFAULT_DB))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开只读连接：WAL 模式、row_factory=Row。

    db_path 未传时优先取 BACKTEST_DB 环境变量（测试注入临时库），否则 data/market.db。
    """
    path = Path(db_path) if db_path else Path(_default_db())
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn
