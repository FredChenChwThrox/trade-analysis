"""测试共享 fixture：UI 临时库/客户端 + backtest 合成行情库。"""

import pytest

from scripts.pipeline import db as pipeline_db

from tests.ui_seed import seed_ui_data


@pytest.fixture()
def ui_db_path(tmp_path):
    """返回已建库并填好合成数据的临时 DB 路径。"""
    path = tmp_path / "market.db"
    conn = pipeline_db.connect(path)
    pipeline_db.migrate(conn)
    pipeline_db.seed(conn)
    seed_ui_data(conn)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def ui_conn(ui_db_path):
    """UI 查询用连接（只读，row_factory 已配置）。"""
    from scripts.ui.db import get_connection

    conn = get_connection(ui_db_path)
    yield conn
    conn.close()


@pytest.fixture()
def client(ui_db_path):
    """Flask 测试客户端（绑定临时库）。"""
    from scripts.ui.app import create_app

    app = create_app(db_path=ui_db_path)
    app.config["TESTING"] = True
    return app.test_client()


# ---------------- backtest 合成多股库（Phase 2/3 共享） ----------------

_BT_MULTI_SYMS = ("000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ")
_BT_MULTI_DRIFT = {"000001.SZ": 0.004, "000002.SZ": 0.001,
                   "000003.SZ": 0.0, "000004.SZ": -0.003}
_BT_N_BARS = 55
_BT_IDX_DATES = None  # 惰性初始化


def _bt_gen_closes(drift: float, n: int) -> list[float]:
    closes = [10.0] * 25
    c = 10.0
    for _ in range(n - 25):
        c *= 1 + drift
        closes.append(c)
    return closes


@pytest.fixture()
def bt_multi_db_path(tmp_path):
    """4 只差异化趋势股票 + 000300.SH 基准指数的临时回测库。"""
    import pandas as pd

    global _BT_IDX_DATES
    idx_dates = pd.bdate_range("2024-01-02", periods=_BT_N_BARS)
    _BT_IDX_DATES = idx_dates

    path = tmp_path / "market_multi.db"
    conn = pipeline_db.connect(path)
    pipeline_db.migrate(conn)
    for sym in _BT_MULTI_SYMS:
        conn.execute(
            "INSERT INTO watchlist (symbol, market, name, aliases_json,"
            " benchmark_code, currency, timezone, active, created_at, updated_at)"
            f" VALUES ('{sym}', 'CN', '{sym}', '[]', '000300.SH', 'CNY',"
            " 'Asia/Shanghai', 1, '2024-01-01', '2024-01-01')")
    calendar = (pipeline_db.CONFIG_DIR / "calendar_cn_2024.yaml")
    pipeline_db.seed_calendar(conn, calendar)
    # 指数基准 close=3000+i
    for i, d in enumerate(idx_dates):
        conn.execute(
            """
            INSERT INTO index_bars (index_code, trade_date, open, high, low, close,
                                    volume, currency, source, available_at, updated_at)
            VALUES ('000300.SH', ?, ?, ?, ?, ?, NULL, 'CNY', 'test',
                    '2024-01-02', '2024-01-02')
            """, (d.date().isoformat(), 3000 + i, 3010 + i, 2990 + i, 3000 + i))
    # daily_bars 直插：date 纯日期字符串；amount 刻意留 NULL（Phase 2 场景）
    for sym in _BT_MULTI_SYMS:
        drift = _BT_MULTI_DRIFT[sym]
        for d, c in zip(idx_dates, _bt_gen_closes(drift, _BT_N_BARS)):
            conn.execute(
                """
                INSERT INTO daily_bars (symbol, trade_date, market,
                    open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                    currency, price_adj_factor, share_factor, trading_status,
                    source, raw_object_id, updated_at)
                VALUES (?, ?, 'CN', ?, ?, ?, ?, ?,
                        NULL, 'CNY', 1.0, 1.0, 'normal', 'test', NULL, '2024-01-01')
                """, (sym, d.date().isoformat(), c * 0.99, c * 1.005,
                      c * 0.995, c, 100000))
    conn.commit()
    conn.close()
    return path
