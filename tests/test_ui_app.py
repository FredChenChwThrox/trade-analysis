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
