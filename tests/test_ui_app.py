"""任务 00/11：UI 应用入口与健康检查（/health）。"""

from scripts.ui import db as ui_db

from tests.ui_seed import EXPECTED_STOCK_TOTAL


def test_health_returns_ok_and_db_status(client):
    rv = client.get("/health")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["status"] == "ok"
    assert "db_path" in data


def test_health_reports_db_not_ok_when_missing(client, tmp_path, monkeypatch):
    """指向不存在的库文件时 /health 返回 error。"""
    from scripts.ui.app import create_app

    app = create_app(db_path=tmp_path / "no_such.db")
    app.config["TESTING"] = True
    rv = app.test_client().get("/health")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "error"


def test_get_connection_row_factory(ui_db_path):
    conn = ui_db.get_connection(ui_db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()
        assert row["n"] == EXPECTED_STOCK_TOTAL  # watchlist.yaml CN 股 + 补种 1 HK
    finally:
        conn.close()


def test_ui_config_loads():
    from scripts.ui.config import load_ui_config

    cfg = load_ui_config()
    assert cfg["app"]["host"] == "127.0.0.1"
    assert cfg["app"]["port"] == 5000
    assert cfg["defaults"]["page_size"] == 50
    assert cfg["charts"]["library"] == "echarts"


# ---------------------------------------------------------------- 日历横幅（r2 Phase 1）

def test_cards_page_calendar_banner(client, ui_conn):
    """/cards 顶部横幅渲染 event_calendar 到期项（与复核到期并列）。"""
    ui_conn.execute(
        """
        INSERT INTO event_calendar (cal_id, kind, symbol, scheduled_date, source,
                                    remind_before_days, note, raw_object_id, ingested_at)
        VALUES ('cal_test_ui', 'unlock', '000001.SZ', date('now', '+1 day'),
                'manual', 3, '解禁 1.00 亿股', NULL, 'test')
        """)
    ui_conn.commit()
    rv = client.get("/cards")
    assert rv.status_code == 200
    html = rv.get_data(as_text=True)
    assert "日历提醒" in html
    assert "解禁 1.00 亿股" in html


def test_cards_page_no_banner_when_nothing_due(tmp_path):
    """空态：无任何到期项时横幅不渲染（不占位）。"""
    from scripts.pipeline import db as pipeline_db
    from scripts.ui.app import create_app

    path = tmp_path / "empty.db"
    conn = pipeline_db.connect(path)
    pipeline_db.migrate(conn)
    pipeline_db.seed(conn)
    conn.execute("DELETE FROM event_calendar")  # 消除种子日期对"今天"的依赖
    conn.commit()
    conn.close()
    app = create_app(db_path=path)
    app.config["TESTING"] = True
    rv = app.test_client().get("/cards")
    assert rv.status_code == 200
    assert "日历提醒（默认 3 日内" not in rv.get_data(as_text=True)
