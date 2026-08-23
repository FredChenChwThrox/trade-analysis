"""UI 测试共享 fixture：临时库（migrate + seed + 合成数据）与 Flask 测试客户端。"""

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
