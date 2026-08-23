# Task 00 完成记录

完成日期：2026-08-10

## 实现摘要

- 目录结构：`scripts/ui/{templates/partials,static/css,static/js}`、`docs/tasks/progress/`。
- `config/ui.yaml`：app(host/port/debug)、defaults(page_size/recent_trading_days/default_indicators)、price_display、charts 配色（docs/ui_design_phase1.md §6）。
- `scripts/ui/__init__.py`、`scripts/ui/db.py`（`get_connection`，路径优先级：显式 > `TRADE_DB_PATH` 环境变量 > `data/market.db`，复用 `scripts/pipeline/db.py.connect`）、`scripts/ui/config.py`（`load_ui_config`）。
- `scripts/ui/app.py`：`create_app(db_path, ui_config)` 工厂（供测试注入临时库）+ `main()` CLI；`GET /health` 返回数据库状态；404/500 error handler（API 返回 JSON）。
- `pyproject.toml` 增加 `flask>=3.0.0`。

## 测试

- `tests/conftest.py`：`ui_db_path`（临时库 migrate+seed+合成数据）、`ui_conn`、`client` fixtures。
- `tests/ui_seed.py`：确定性合成数据（6+1 只股票、30 交易日、因子 1.0/2.0 两档、信号、卡片、执行、运行、报告），供后续全部任务复用。
- `tests/test_ui_app.py` 4 项：/health ok、/health error（库缺失）、get_connection row_factory、ui.yaml 可加载。

## 问题与决策

- Flask app 采用 `create_app` 工厂模式，避免测试与真实库路径耦合（任务验收的 `python -m scripts.ui.app` 由 `main()` 提供）。
- 错误响应：`/api/` 前缀返回 JSON，页面返回 HTML 文本占位（模板在 task-02 落地）。

## 后续建议

- task-01 数据查询层完成后，app.py 再按需注册 API 路由。
