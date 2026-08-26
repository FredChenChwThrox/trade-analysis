"""任务 02/11：布局、全局筛选条、辅助 API 与错误页面。"""


def test_api_markets(client):
    rv = client.get("/api/markets")
    assert rv.status_code == 200
    assert rv.get_json()["markets"] == ["CN", "HK"]


def test_api_stocks_search(client):
    rv = client.get("/api/stocks/search?q=平安")
    data = rv.get_json()["items"]
    # 2026-08-24 加 000001.SZ（平安银行）后按 symbol 字典序排前
    assert data and data[0]["symbol"] == "000001.SZ"
    assert any(it["symbol"] == "601318.SH" for it in data)


def test_api_stocks_search_by_symbol(client):
    rv = client.get("/api/stocks/search?q=002747")
    data = rv.get_json()["items"]
    assert data[0]["symbol"] == "002747.SZ"


def test_api_stocks_search_empty_returns_all(client):
    rv = client.get("/api/stocks/search?limit=3")
    assert len(rv.get_json()["items"]) == 3


def test_static_assets_load(client):
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/common.js").status_code == 200


def test_base_page_renders(client):
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "股票监测系统" in body
    assert "app.css" in body
    assert "common.js" in body


def test_navbar_present(client):
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    # 导航 = 股票下拉框（全部 watchlist）+ 数据入口
    assert 'id="stock-select"' in body
    assert '<option value="603605.SH"' in body
    assert "珀莱雅" in body
    assert 'href="/data"' in body
    # 旧导航项已下线
    for label in ["股票列表", "指标分析", "信号时间轴", "多股对比", "卡片列表", "运行状态"]:
        assert label not in body


def test_navbar_highlights_current_stock(client):
    body = client.get("/stock/601318.SH").get_data(as_text=True)
    assert '<option value="601318.SH" selected>' in body


def test_404_page(client):
    rv = client.get("/no-such-page")
    assert rv.status_code == 404
    assert "页面不存在" in rv.get_data(as_text=True)


def test_404_api_returns_json(client):
    rv = client.get("/api/no-such-endpoint")
    assert rv.status_code == 404
    assert rv.get_json()["error"] == "not found"


def test_common_js_helpers_present(client):
    js = client.get("/static/js/common.js").get_data(as_text=True)
    for fn in ["formatDate", "formatNumber", "buildQueryString", "debounce",
               "fetchJSON", "renderStatusBadge", "initDateRangePicker", "initStockSearch"]:
        assert fn in js
