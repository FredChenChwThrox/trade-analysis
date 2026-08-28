"""Flask 只读 Web UI 入口（第一期，docs/ui_design_phase1.md §2/§7）。

CLI：uv run python -m scripts.ui.app

所有 API 只读；符号不写死，全部来自数据库或用户输入；静态/模板不暴露 .db / data/raw。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from importlib.metadata import version
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request

from scripts.ui.config import load_ui_config
from scripts.ui.db import DEFAULT_DB_PATH, get_connection

# 当前 app 的库路径（create_app 注入，get_db 使用）
_UI_DB_PATH: str = ""


def get_db():
    """当前请求上下文内的只读连接（teardown 时关闭）。"""
    if "ui_conn" not in g:
        g.ui_conn = get_connection(_UI_DB_PATH)
    return g.ui_conn


def create_app(db_path: str | Path | None = None, ui_config: dict | None = None) -> Flask:
    global _UI_DB_PATH

    app = Flask(__name__)
    app.config["UI_CONFIG"] = ui_config or load_ui_config()
    _UI_DB_PATH = str(db_path or _resolve_default_db())
    app.config["DB_PATH"] = _UI_DB_PATH
    app.json.ensure_ascii = False

    @app.teardown_appcontext
    def _close(exc):  # noqa: ANN001
        conn = g.pop("ui_conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def inject_footer():
        from scripts.ui import queries

        try:
            last_bar = get_db().execute("SELECT MAX(updated_at) AS u FROM daily_bars").fetchone()["u"]
            last_run = get_db().execute(
                "SELECT MAX(finished_at) AS u FROM pipeline_runs").fetchone()["u"]
        except sqlite3.Error:
            last_bar = last_run = None
        try:
            nav = [{"symbol": r["symbol"], "name": r["name"]}
                   for r in sorted(queries.get_watchlist(get_db()),
                                   key=lambda x: (x["market"], x["symbol"]))]
        except sqlite3.Error:
            nav = []
        try:
            flask_ver = version("flask")
        except Exception:  # noqa: BLE001
            flask_ver = "?"
        return {"footer": {
            "db_path": _UI_DB_PATH, "db_last_update": last_bar, "run_last": last_run,
            "flask_version": flask_ver,
        }, "nav_stocks": nav}

    def _fmt_num(v, precision: int = 2) -> str:
        """模板数字格式化：千分位 + 缺失占位（不猜，缺数据显示"—"）。"""
        if v is None or v == "":
            return "—"
        try:
            return f"{float(v):,.{precision}f}"
        except (TypeError, ValueError):
            return str(v)

    app.add_template_filter(_fmt_num, "fmt")

    @app.get("/health")
    def health():
        try:
            n = get_db().execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
            return {"status": "ok", "db_path": _UI_DB_PATH, "watchlist_count": n}
        except sqlite3.Error as exc:
            return {"status": "error", "db_path": _UI_DB_PATH, "error": str(exc)}

    _register_routes(app)

    @app.errorhandler(404)
    def not_found(_e):  # noqa: ANN001
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found", "code": 404}), 404
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):  # noqa: ANN001
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal error", "code": 500}), 500
        return render_template("500.html"), 500

    return app


def _register_routes(app: Flask) -> None:
    from scripts.ui import parse_args as pa
    from scripts.ui import queries

    @app.get("/api/markets")
    def api_markets():
        return {"markets": queries.list_markets(get_db())}

    @app.get("/api/stocks/search")
    def api_stocks_search():
        q = (request.args.get("q") or "").strip()
        limit = min(int(request.args.get("limit", 10)), 50)
        return {"items": queries.search_stocks(get_db(), q, limit=limit)}

    @app.get("/api/stocks")
    def api_stocks():
        try:
            filters = pa.stock_filters(request.args)
            page = pa.int_arg(request.args, "page", 1)
            page_size = min(pa.int_arg(request.args, "page_size", 50), 200)
            sort = pa.sort_arg(request.args, "sort", "latest_trade_date",
                               {"latest_trade_date", "latest_close", "pe_ttm",
                                "pct_chg", "signal_count_5d"})
            order = pa.order_arg(request.args, "desc")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return jsonify(queries.list_stocks(get_db(), filters, page=page, page_size=page_size,
                                           sort=sort, order=order))

    @app.get("/api/stocks/<symbol>")
    def api_stock_meta(symbol):
        meta = queries.get_stock_meta(get_db(), symbol)
        if meta is None:
            return jsonify({"error": f"unknown symbol: {symbol}", "code": 404}), 404
        return jsonify(meta)

    @app.get("/api/stocks/<symbol>/overview")
    def api_stock_overview(symbol):
        try:
            limit = min(pa.int_arg(request.args, "event_limit", 20), 100)
            offset = pa.int_arg(request.args, "event_offset", 0)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        data = queries.get_stock_overview(get_db(), symbol,
                                          event_limit=limit, event_offset=offset)
        if data is None:
            return jsonify({"error": f"unknown symbol: {symbol}", "code": 404}), 404
        return jsonify(data)

    @app.get("/api/stocks/<symbol>/bars")
    def api_stock_bars(symbol):
        try:
            start, end = pa.date_range(request.args, default_days=90)
            price = pa.price_mode(request.args, "unadjusted")
            granularity = pa.granularity(request.args)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return {"symbol": symbol, "granularity": granularity, "price": price,
                "bars": queries.get_stock_bars(get_db(), symbol, granularity, start, end, price)}

    @app.get("/api/stocks/<symbol>/indicators")
    def api_stock_indicators(symbol):
        try:
            start, end = pa.date_range(request.args, default_days=90)
            fields = pa.fields_arg(request.args, max_fields=None)
            granularity = pa.granularity(request.args)
            price = pa.price_mode(request.args, "unadjusted")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        try:
            rows = queries.get_stock_indicators(get_db(), symbol, granularity,
                                                start, end, fields, price)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return {"symbol": symbol, "granularity": granularity, "price": price,
                "fields": fields or None, "indicators": rows}

    @app.get("/api/stocks/<symbol>/signals")
    def api_stock_signals(symbol):
        try:
            start, end = pa.date_range(request.args, default_days=90)
            limit = pa.int_arg(request.args, "limit", 100)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        data = queries.list_signals(get_db(), {"symbols": [symbol], "start": start, "end": end},
                                    page=1, page_size=limit, sort="observed_on", order="desc")
        return {"symbol": symbol, **data}

    @app.get("/api/stocks/<symbol>/cards")
    def api_stock_cards(symbol):
        data = queries.list_cards(get_db(), {"symbol": symbol}, page=1, page_size=100)
        return {"symbol": symbol, **data}

    @app.get("/api/stocks/<symbol>/executions")
    def api_stock_executions(symbol):
        data = queries.list_executions(get_db(), symbol=symbol)
        return {"symbol": symbol, **data}

    @app.get("/api/signals")
    def api_signals():
        try:
            filters = pa.signal_filters(request.args)
            page = pa.int_arg(request.args, "page", 1)
            page_size = pa.int_arg(request.args, "page_size", 100)
            sort = pa.sort_arg(request.args, "sort", "observed_on",
                               {"observed_on", "symbol", "signal", "state", "triggered"})
            order = pa.order_arg(request.args, "desc")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return jsonify(queries.list_signals(get_db(), filters, page=page,
                                            page_size=page_size, sort=sort, order=order))

    @app.get("/api/signals/<int:fact_id>")
    def api_signal_detail(fact_id):
        detail = queries.get_signal_details(get_db(), fact_id)
        if detail is None:
            return jsonify({"error": f"unknown fact_id: {fact_id}", "code": 404}), 404
        return jsonify(detail)

    @app.get("/api/indicators")
    def api_indicators():
        try:
            symbols = pa.symbol_list_arg(request.args, "symbols", max_n=6, required=True)
            fields = pa.fields_arg(request.args, max_fields=6, required=True)
            start, end = pa.date_range(request.args, default_days=90)
            granularity = pa.granularity(request.args)
            price = pa.price_mode(request.args, "unadjusted")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        try:
            data = queries.get_multi_indicators(get_db(), symbols, granularity,
                                                start, end, fields, price)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return jsonify(data)

    @app.get("/api/compare")
    def api_compare():
        try:
            symbols = pa.symbol_list_arg(request.args, "symbols", max_n=6, required=True)
            metric = (request.args.get("metric") or "").strip()
            if not metric:
                raise pa.ParseError("参数 metric 必填")
            if "," in metric:
                raise pa.ParseError("compare 只支持单个指标 metric")
            start, end = pa.date_range(request.args, default_days=90)
            granularity = pa.granularity(request.args)
            price = pa.price_mode(request.args, "unadjusted")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        try:
            data = queries.get_compare(get_db(), symbols, metric, granularity,
                                       start, end, price)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return jsonify(data)

    @app.get("/api/cards")
    def api_cards():
        try:
            filters = pa.card_filters(request.args)
            page = pa.int_arg(request.args, "page", 1)
            page_size = pa.int_arg(request.args, "page_size", 50)
            sort = pa.sort_arg(request.args, "sort", "created_at",
                               {"created_at", "effective_from", "status"})
            order = pa.order_arg(request.args, "desc")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return jsonify(queries.list_cards(get_db(), filters, page=page,
                                          page_size=page_size, sort=sort, order=order))

    @app.get("/api/cards/<card_version_id>")
    def api_card_detail(card_version_id):
        detail = queries.get_card_detail(get_db(), card_version_id)
        if detail is None:
            return jsonify({"error": f"unknown card: {card_version_id}", "code": 404}), 404
        return jsonify(detail)

    @app.get("/api/runs")
    def api_runs():
        try:
            filters = pa.run_filters(request.args)
            page = pa.int_arg(request.args, "page", 1)
            page_size = pa.int_arg(request.args, "page_size", 50)
            sort = pa.sort_arg(request.args, "sort", "started_at",
                               {"started_at", "status", "stage"})
            order = pa.order_arg(request.args, "desc")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return jsonify(queries.list_pipeline_runs(get_db(), filters, page=page,
                                                  page_size=page_size, sort=sort, order=order))

    @app.get("/api/reports")
    def api_reports():
        try:
            filters = pa.report_filters(request.args)
            page = pa.int_arg(request.args, "page", 1)
            page_size = pa.int_arg(request.args, "page_size", 50)
            sort = pa.sort_arg(request.args, "sort", "created_at",
                               {"created_at", "trade_date", "status"})
            order = pa.order_arg(request.args, "desc")
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": 400}), 400
        return jsonify(queries.list_report_runs(get_db(), filters, page=page,
                                                page_size=page_size, sort=sort, order=order))

    @app.get("/api/dashboard")
    def api_dashboard():
        return jsonify(queries.get_dashboard(get_db()))

    @app.get("/reports/<path:filepath>")
    def serve_report(filepath):
        """只读浏览 reports/ 下的报告 Markdown（禁止越出 reports 目录）。"""
        root = (Path(_UI_DB_PATH).resolve().parent.parent / "reports").resolve()
        target = (root / filepath).resolve()
        if not str(target).startswith(str(root)) or not target.is_file() \
                or target.suffix not in (".md", ".markdown"):
            return jsonify({"error": "not found", "code": 404}), 404
        from flask import Response

        return Response(target.read_text(encoding="utf-8"), mimetype="text/markdown")

    # ---------------------------------------------------------------- 页面路由
    @app.get("/")
    def page_home():
        launcher = queries.list_stocks(get_db(), {}, page=1, page_size=200)["items"]
        run_alerts = queries.list_run_alerts(get_db())
        return render_template("index.html", cfg=app.config["UI_CONFIG"],
                               active="home", stocks=launcher, run_alerts=run_alerts)

    @app.get("/stocks")
    def page_stocks():
        # 旧股票列表页已由首页启动台取代
        return redirect("/", code=302)

    @app.get("/stock/<symbol>")
    def page_stock(symbol):
        meta = queries.get_stock_meta(get_db(), symbol)
        if meta is None:
            return render_template("404.html", reason=f"未知股票 {symbol}"), 404
        return render_template("stock.html", cfg=app.config["UI_CONFIG"],
                               active="stock", symbol=symbol, meta=meta)

    @app.get("/data")
    def page_data():
        return render_template("data.html", cfg=app.config["UI_CONFIG"], active="data")

    @app.get("/indicators")
    def page_indicators():
        return render_template("indicators.html", cfg=app.config["UI_CONFIG"], active="indicators")

    @app.get("/signals")
    def page_signals():
        return render_template("signals.html", cfg=app.config["UI_CONFIG"], active="signals")

    @app.get("/compare")
    def page_compare():
        return render_template("compare.html", cfg=app.config["UI_CONFIG"], active="compare")

    @app.get("/cards")
    def page_cards():
        # r2 Phase 1：日历提醒横幅（卡片复核到期 + event_calendar 到期项，全池级）
        calendar_alerts = [a for a in queries.get_dashboard_alerts(get_db())
                           if a.get("type") in ("review_due", "calendar")]
        return render_template("cards.html", cfg=app.config["UI_CONFIG"],
                               active="cards", calendar_alerts=calendar_alerts)

    @app.get("/runs")
    def page_runs():
        return render_template("runs.html", cfg=app.config["UI_CONFIG"], active="runs")

    # ---------------------------------------------------------------- 消息面人审（r2 Phase 3）
    @app.get("/message-review")
    def page_message_review():
        rows = queries.list_message_review(get_db())
        return render_template("message_review.html", cfg=app.config["UI_CONFIG"],
                               active="message_review", rows=rows)

    @app.post("/message-review/<event_id>/action")
    def message_review_action(event_id: str):
        """人审动作落 event_human_review（不改写原始 LLM 行，r2 §3.3）。"""
        import json
        from scripts.pipeline.db import utc_now

        action = request.form.get("action") or ""
        if action not in ("confirm", "dismiss", "upgrade_materiality", "note", "amend"):
            return jsonify({"error": "unknown action", "code": 400}), 400
        symbol = request.form.get("symbol") or "__event__"
        payload = {k: request.form.get(k) for k in
                   ("materiality", "note", "expectation_gap", "falsification",
                    "target", "half_life") if request.form.get(k)}
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO event_human_review (event_id, symbol, action, "
                "payload_json, actor, reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, symbol, action,
                 json.dumps(payload, ensure_ascii=False) if payload else None,
                 request.form.get("actor") or "manual", utc_now()))
        return redirect("/message-review")


def _resolve_default_db() -> str:
    return os.environ.get("TRADE_DB_PATH") or str(DEFAULT_DB_PATH)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scripts.ui.app")
    parser.add_argument("--db", default=None, help="数据库文件路径")
    parser.add_argument("--config", default=None, help="config/ui.yaml 路径")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None, type=int)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_ui_config(args.config) if args.config else load_ui_config()
    host = args.host or cfg["app"]["host"]
    port = args.port or cfg["app"]["port"]
    app = create_app(db_path=args.db, ui_config=cfg)
    app.run(host=host, port=port, debug=args.debug)


if __name__ == "__main__":
    main()
