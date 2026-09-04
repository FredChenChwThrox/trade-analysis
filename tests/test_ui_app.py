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


def test_message_review_page_and_action(client, ui_conn):
    """r2 Phase 3：/message-review 渲染 LLM 评价 + 人审动作落 event_human_review。"""
    now = "2026-08-28T10:00:00+00:00"
    ui_conn.execute(
        "INSERT INTO events (event_id, event_type, published_at, published_tz,"
        " available_at, title, summary, source, content_hash, ingested_at,"
        " source_tier) VALUES ('evt_ui', 'news', ?, 'Asia/Shanghai', ?,"
        " 'LME铜 大跌', NULL, 'akshare', 'h', ?, 4)", (now, now, now))
    ui_conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES ('evt_ui', '000001.SZ')")
    # 池外关联股：名称走 symbol_names 目录兜底（0006 migration）
    ui_conn.execute(
        "INSERT INTO symbol_names (symbol, name, source, ingested_at)"
        " VALUES ('000776.SZ', '广发证券', 'eastmoney_em', ?)", (now,))
    ui_conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES ('evt_ui', '000776.SZ')")
    ui_conn.execute(
        "INSERT INTO event_assessments (event_id, symbol, assessment_version,"
        " model, prompt_version, assessed_at, event_type, direction, materiality,"
        " confidence, rationale, status, run_id)"
        " VALUES ('evt_ui', '__event__', 'llm_v1', 'fake', 'llm_v1', ?, 'news',"
        " 'negative', 'medium', 0.7, '库存上升', 'needs_review', 'r')", (now,))
    ui_conn.commit()

    rv = client.get("/message-review")
    assert rv.status_code == 200
    html = rv.get_data(as_text=True)
    assert "LME铜 大跌" in html and "待人审" in html
    assert "000001.SZ 平安银行" in html  # 关联股带名称（watchlist）
    assert "000776.SZ 广发证券" in html  # 池外股名称走 symbol_names 兜底
    assert 'data-symbols="000001.SZ|000776.SZ"' in html  # 公司过滤 data 属性（| 分隔）
    assert '<option value="000001.SZ">000001.SZ 平安银行</option>' in html  # 公司下拉
    assert '<option value="000776.SZ">000776.SZ 广发证券</option>' in html

    rv = client.post("/message-review/evt_ui/action",
                     data={"action": "confirm", "symbol": "__event__",
                           "actor": "fred"}, follow_redirects=True)
    assert "已过审" in client.get("/message-review").get_data(as_text=True)


def test_message_review_confirm_all(client, ui_conn):
    """一键确认：全部待人审事件落 confirm，已过审事件不受影响；company 参数跟随公司筛选。"""
    now = "2026-08-28T10:00:00+00:00"
    for eid, st, sym in (("evt_ca", "needs_review", "000001.SZ"),
                         ("evt_cb", "needs_review", "000333.SZ"),
                         ("evt_cc", "ok", None)):
        ui_conn.execute(
            "INSERT INTO events (event_id, event_type, published_at, published_tz,"
            " available_at, title, summary, source, content_hash, ingested_at,"
            " source_tier) VALUES (?, 'news', ?, 'Asia/Shanghai', ?,"
            " ?, NULL, 'akshare', ?, ?, 4)",
            (eid, now, now, "标题" + eid, "h_" + eid, now))
        ui_conn.execute(
            "INSERT INTO event_assessments (event_id, symbol, assessment_version,"
            " model, prompt_version, assessed_at, event_type, direction, materiality,"
            " confidence, rationale, status, run_id)"
            " VALUES (?, '__event__', 'llm_v1', 'fake', 'llm_v1', ?, 'news',"
            " 'negative', 'medium', 0.7, 'r', ?, 'r')", (eid, now, st))
        if sym:
            ui_conn.execute(
                "INSERT INTO event_symbols (event_id, symbol) VALUES (?, ?)",
                (eid, sym))
    ui_conn.commit()

    # 带 company：只确认关联 000001.SZ 的待人审事件
    rv = client.post("/message-review/confirm-all",
                     data={"actor": "fred", "company": "000001.SZ"},
                     follow_redirects=True)
    assert rv.status_code == 200
    confirmed = {r["event_id"] for r in ui_conn.execute(
        "SELECT event_id FROM event_human_review WHERE action = 'confirm'")}
    assert confirmed == {"evt_ca"}

    # 不带 company：确认剩余全部待人审
    rv = client.post("/message-review/confirm-all", data={"actor": "fred"},
                     follow_redirects=True)
    assert rv.status_code == 200
    confirmed = {r["event_id"] for r in ui_conn.execute(
        "SELECT event_id FROM event_human_review WHERE action = 'confirm'")}
    assert confirmed == {"evt_ca", "evt_cb"}  # ok 条不受影响
    html = client.get("/message-review").get_data(as_text=True)
    assert "一键确认待人审（0）" in html


def test_message_review_confirm_all_skips_dismissed(client, ui_conn):
    """confirm-all 只处理 needs_review 且未否决/未确认的行：已否决事件不被重新确认。"""
    now = "2026-08-28T10:00:00+00:00"
    ui_conn.execute(
        "INSERT INTO events (event_id, event_type, published_at, published_tz,"
        " available_at, title, summary, source, content_hash, ingested_at,"
        " source_tier) VALUES ('evt_d', 'news', ?, 'Asia/Shanghai', ?,"
        " '已否决事件', NULL, 'akshare', 'h', ?, 4)", (now, now, now))
    ui_conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES ('evt_d', '000001.SZ')")
    ui_conn.execute(
        "INSERT INTO event_assessments (event_id, symbol, assessment_version,"
        " model, prompt_version, assessed_at, event_type, direction, materiality,"
        " confidence, rationale, status, run_id)"
        " VALUES ('evt_d', '__event__', 'llm_v1', 'fake', 'llm_v1', ?, 'news',"
        " 'negative', 'medium', 0.7, 'r', 'needs_review', 'r')", (now,))
    # 基础 status 仍是 needs_review，但已有 dismiss 记录（人审否决）
    ui_conn.execute(
        "INSERT INTO event_human_review (event_id, symbol, action, payload_json,"
        " actor, reviewed_at) VALUES ('evt_d', '__event__', 'dismiss', NULL,"
        " 'fred', ?)", (now,))
    ui_conn.commit()

    rv = client.post("/message-review/confirm-all", data={"actor": "fred"},
                     follow_redirects=True)
    assert rv.status_code == 200
    n = ui_conn.execute(
        "SELECT COUNT(*) AS c FROM event_human_review WHERE event_id = 'evt_d'"
        " AND action = 'confirm'").fetchone()["c"]
    assert n == 0  # 已否决事件不因基础 status 仍 needs_review 被一键确认


def test_message_review_unknown_action_rejected(client):
    rv = client.post("/message-review/evt_x/action",
                     data={"action": "hack"})
    assert rv.status_code == 400
