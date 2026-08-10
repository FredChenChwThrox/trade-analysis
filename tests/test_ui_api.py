"""任务 11：UI API 端到端测试（HTTP 200、JSON 结构、筛选生效、口径一致、参数校验）。"""


def test_api_stocks_default(client):
    rv = client.get("/api/stocks")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["total"] == 7
    assert data["page"] == 1
    assert data["items"][0]["symbol"]


def test_api_stocks_filters(client):
    rv = client.get("/api/stocks?market=HK")
    assert [it["symbol"] for it in rv.get_json()["items"]] == ["0700.HK"]
    rv = client.get("/api/stocks?q=珀莱雅")
    assert [it["symbol"] for it in rv.get_json()["items"]] == ["603605.SH"]
    rv = client.get("/api/stocks?has_active_card=1")
    assert [it["symbol"] for it in rv.get_json()["items"]] == ["603605.SH"]
    rv = client.get("/api/stocks?pe_min=20&pe_max=30")
    assert [it["symbol"] for it in rv.get_json()["items"]] == ["002747.SZ"]
    rv = client.get("/api/stocks?recent_signal_days=5")
    assert [it["symbol"] for it in rv.get_json()["items"]] == ["603605.SH"]


def test_api_stocks_sort_pagination(client):
    rv = client.get("/api/stocks?sort=latest_close&order=desc&page=1&page_size=2")
    data = rv.get_json()
    assert data["page_size"] == 2
    assert data["total"] == 7
    assert data["items"][0]["symbol"] == "0700.HK"


def test_api_stock_meta(client):
    rv = client.get("/api/stocks/603605.SH")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["name"] == "珀莱雅"
    assert data["latest"]["close_raw"] == 79.0
    assert data["active_card"]["card_version_id"] == "603605SH_120ca661"


def test_api_stock_meta_unknown_404(client):
    rv = client.get("/api/stocks/NOPE.X")
    assert rv.status_code == 404


def test_api_bars_price_modes(client):
    # 完全复权 = 不复权 × 因子（002747.SZ 因子 2.0）
    raw = client.get("/api/stocks/002747.SZ/bars?granularity=daily&price=unadjusted").get_json()
    adj = client.get("/api/stocks/002747.SZ/bars?granularity=daily&price=fully_adjusted").get_json()
    assert raw["bars"][-1]["close"] == 49.0
    assert adj["bars"][-1]["close"] == 98.0


def test_api_bars_weekly(client):
    rv = client.get("/api/stocks/002747.SZ/bars?granularity=weekly&price=fully_adjusted")
    bars = rv.get_json()["bars"]
    assert bars[-1]["close"] == 49.0
    raw = client.get("/api/stocks/002747.SZ/bars?granularity=weekly&price=unadjusted").get_json()
    assert raw["bars"][-1]["close"] == 49.0  # 聚合不复权周线收盘 = 周末 close_raw
    assert raw["bars"][-1]["open"] == 44.5


def test_api_indicators_折回(client):
    # 不复权：MA 折回（存储 94.0 ÷ 因子 2.0 = 47.0）；完全复权：保留 94.0
    rv = client.get(
        "/api/stocks/002747.SZ/indicators?fields=ma5&price=unadjusted&start=2026-08-07&end=2026-08-07")
    assert rv.get_json()["indicators"][0]["ma5"] == 47.0
    rv = client.get(
        "/api/stocks/002747.SZ/indicators?fields=ma5&price=fully_adjusted&start=2026-08-07&end=2026-08-07")
    assert rv.get_json()["indicators"][0]["ma5"] == 94.0


def test_api_indicators_invalid_field_400(client):
    rv = client.get("/api/stocks/603605.SH/indicators?fields=not_a_column")
    assert rv.status_code == 400


def test_api_stock_signals(client):
    rv = client.get("/api/stocks/603605.SH/signals")
    assert rv.status_code == 200
    assert rv.get_json()["total"] == 4


def test_api_stock_cards_and_executions(client):
    cards = client.get("/api/stocks/603605.SH/cards").get_json()
    assert cards["total"] == 4
    execs = client.get("/api/stocks/603605.SH/executions").get_json()
    assert execs["total"] == 4


def test_api_signals_filters(client):
    rv = client.get("/api/signals?symbols=603605.SH&triggered=1")
    data = rv.get_json()
    assert data["total"] == 3
    assert all(it["triggered"] for it in data["items"])
    rv = client.get("/api/signals?states=incomplete")
    assert rv.get_json()["total"] == 2


def test_api_signal_detail(client):
    data = client.get("/api/signals?signals=panic").get_json()
    fact_id = data["items"][0]["fact_id"]
    rv = client.get(f"/api/signals/{fact_id}")
    assert rv.status_code == 200
    assert rv.get_json()["anchor"]["anchor_type"] == "panic_low"


def test_api_indicators_multi(client):
    rv = client.get(
        "/api/indicators?symbols=603605.SH,002747.SZ&fields=ma5,pe_ttm"
        "&start=2026-08-06&end=2026-08-07")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["fields"] == ["ma5", "pe_ttm"]
    assert "ma5" in data["series"]
    assert data["series"]["ma5"]["2026-08-07"]["603605.SH"] is not None


def test_api_indicators_multi_limit_400(client):
    rv = client.get("/api/indicators?symbols=a,b,c,d,e,f,g&fields=ma5")
    assert rv.status_code == 400


def test_api_compare(client):
    rv = client.get(
        "/api/compare?symbols=603605.SH,002747.SZ&metric=pe_ttm&start=2026-08-01&end=2026-08-07")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["metric"] == "pe_ttm"
    assert set(data["series"]) == {"603605.SH", "002747.SZ"}
    assert data["metadata"]["603605.SH"]["name"] == "珀莱雅"


def test_api_compare_close_metric(client):
    rv = client.get(
        "/api/compare?symbols=603605.SH,002747.SZ&metric=close&price=fully_adjusted"
        "&start=2026-08-06&end=2026-08-07")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["series"]["002747.SZ"] == [96.0, 98.0]


def test_api_compare_too_few_symbols_400(client):
    rv = client.get("/api/compare?symbols=603605.SH&metric=pe_ttm")
    assert rv.status_code == 400


def test_api_cards(client):
    rv = client.get("/api/cards?status=active")
    data = rv.get_json()
    assert data["total"] == 1
    assert data["items"][0]["card_version_id"] == "603605SH_120ca661"


def test_api_card_detail(client):
    rv = client.get("/api/cards/603605SH_120ca661")
    data = rv.get_json()
    assert data["price_tiers_json"]["tiers"][0]["zone_low"] == "55.00"
    assert data["version_chain"][0] == "603605SH_120ca661"


def test_api_runs_and_reports(client):
    runs = client.get("/api/runs?status=success").get_json()
    assert runs["total"] >= 7
    assert "duration_sec" in runs["items"][0]
    reports = client.get("/api/reports?report_type=single").get_json()
    assert reports["total"] == 2


def test_api_dashboard(client):
    rv = client.get("/api/dashboard")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["total_stocks"] == 7
    assert data["stocks_with_active_card"] == 1
    assert data["markets"] == ["CN", "HK"]
    assert data["latest_trade_date"] == "2026-08-07"
    assert len(data["run_trend"]) == 7
    assert isinstance(data["alerts"], list)
    assert any(a["type"] == "run_failed" for a in data["alerts"])


# ---------------------------------------------------------------- 参数校验

def test_invalid_date_400(client):
    rv = client.get("/api/stocks/603605.SH/bars?start=2026/08/01")
    assert rv.status_code == 400
    rv = client.get("/api/signals?start=not-a-date")
    assert rv.status_code == 400


def test_invalid_sort_400(client):
    rv = client.get("/api/stocks?sort=name;DROP TABLE")
    assert rv.status_code == 400


def test_invalid_price_mode_400(client):
    rv = client.get("/api/stocks/603605.SH/bars?price=evil")
    assert rv.status_code == 400


def test_invalid_granularity_400(client):
    rv = client.get("/api/stocks/603605.SH/bars?granularity=hourly")
    assert rv.status_code == 400


def test_invalid_page_size(client):
    rv = client.get("/api/stocks?page_size=99999")
    assert rv.status_code == 200  # 上限钳制而不是报错


def test_date_range_reversed_400(client):
    rv = client.get("/api/stocks/603605.SH/bars?start=2026-08-07&end=2026-01-01")
    assert rv.status_code == 400


# ---------------------------------------------------------------- 页面路由

def test_page_stocks(client):
    rv = client.get("/stocks")
    assert rv.status_code == 200
    assert "stocks.js" in rv.get_data(as_text=True)


def test_page_stock(client):
    rv = client.get("/stock/603605.SH")
    assert rv.status_code == 200
    assert "珀莱雅" in rv.get_data(as_text=True)
    assert "stock.js" in rv.get_data(as_text=True)


def test_page_stock_unknown_404(client):
    rv = client.get("/stock/NOPE.X")
    assert rv.status_code == 404


def test_all_page_routes_200(client):
    for path in ["/", "/stocks", "/stock/603605.SH", "/indicators", "/signals",
                 "/compare", "/cards", "/runs"]:
        rv = client.get(path)
        assert rv.status_code == 200, f"{path} -> {rv.status_code}"


def test_page_js_files_load(client):
    for js in ["common", "index", "stocks", "stock", "indicators", "signals",
               "compare", "cards", "runs"]:
        rv = client.get(f"/static/js/{js}.js")
        assert rv.status_code == 200, f"missing {js}.js"


# ---------------------------------------------------------------- 报告文件

def test_report_file_served(client, ui_db_path, tmp_path):
    root = ui_db_path.parent.parent / "reports"
    root.mkdir(parents=True, exist_ok=True)
    (root / "test.md").write_text("# 测试报告", encoding="utf-8")
    rv = client.get("/reports/test.md")
    assert rv.status_code == 200
    assert "# 测试报告" in rv.get_data(as_text=True)


def test_report_file_traversal_blocked(client):
    rv = client.get("/reports/../noescape.md")
    assert rv.status_code == 404


def test_report_non_md_blocked(client, ui_db_path):
    root = ui_db_path.parent.parent / "reports"
    root.mkdir(parents=True, exist_ok=True)
    (root / "evil.py").write_text("print(1)", encoding="utf-8")
    rv = client.get("/reports/evil.py")
    assert rv.status_code == 404


def test_home_page_dashboard_elements(client):
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    assert "home-stats" in body
    assert "run-trend" in body
    assert "index.js" in body
