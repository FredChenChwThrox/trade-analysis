"""UI 只读数据库连接层。

复用 scripts/pipeline/db.py 的连接与路径约定；路径优先级：
显式传入 > 环境变量 TRADE_DB_PATH > 默认 data/market.db。
"""

from __future__ import annotations

import os
from pathlib import Path

from scripts.pipeline.db import DEFAULT_DB_PATH, connect

DB_PATH_ENV = "TRADE_DB_PATH"


def get_connection(db_path: str | Path | None = None):
    """打开只读查询连接（sqlite3.Row row_factory，外键开启）。"""
    path = db_path or os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH
    return connect(path)
