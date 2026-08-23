# 任务 00：项目初始化与环境准备

## 任务目标

建立 UI 项目的目录结构和基础配置，为后续任务提供统一的入口和约定。

## 输入

- `docs/ui_design_phase1.md`
- `pyproject.toml`
- `data/market.db`

## 输出

- `scripts/ui/` 目录结构
- `config/ui.yaml`
- `scripts/ui/__init__.py`
- 更新后的 `pyproject.toml`（新增可选依赖说明，但不强制安装）

## 详细步骤

1. 创建目录：
   - `scripts/ui/`
   - `scripts/ui/templates/`
   - `scripts/ui/static/css/`
   - `scripts/ui/static/js/`
   - `docs/tasks/progress/`

2. 创建 `config/ui.yaml`，包含：
   - 应用 host/port
   - 默认分页
   - 默认日期范围
   - 默认指标列表
   - 图表库选择（默认 echarts）
   - 配色
   - 价格显示模式

3. 创建 `scripts/ui/__init__.py`（空文件即可）。

4. 更新 `pyproject.toml`：
   - 在 `dependencies` 中添加 `flask>=3.0.0`
   - 可选：添加 `fastapi` 作为备选说明（注释）

5. 创建一个最基础的 `scripts/ui/app.py`：
   - 创建 Flask app
   - 加载 `config/ui.yaml`
   - 提供 `/health` 路由，返回数据库连接状态

6. 创建 `scripts/ui/db.py`：
   - 读取 `data/market.db` 路径
   - 提供 `get_connection()` 函数，返回 `sqlite3.Connection`
   - 配置 `row_factory = sqlite3.Row`

7. 在 `docs/tasks/progress/` 创建 `task-00-done.md`，记录完成日期和关键决策。

## 验收标准

- [ ] `python -m scripts.ui.app` 能启动，访问 `http://127.0.0.1:5000/health` 返回数据库状态。
- [ ] 目录结构与 `docs/ui_design_phase1.md` §7 一致。
- [ ] `config/ui.yaml` 能被成功加载。

## 依赖

- 无前置任务。

## 后续任务

- [[task-01-queries]]
