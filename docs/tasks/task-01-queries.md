# 任务 01：数据查询层（queries.py）

## 任务目标

建立 `scripts/ui/queries.py`，封装所有数据库只读查询，为后端 API 提供统一接口。

## 输入

- `docs/ui_design_phase1.md` §4 / §5
- `docs/database_schema.md`
- `data/market.db`

## 输出

- `scripts/ui/queries.py`
- `tests/test_ui_queries.py`（基础查询单元测试）

## 详细步骤

### 1. 基础工具函数

在 `queries.py` 中实现：

- `list_markets() -> list[str]`：返回所有不重复 `market`
- `list_run_ids(limit=100) -> list[dict]`：从 `pipeline_runs` 取最近 `run_id`
- `list_card_status() -> list[str]`：返回卡片状态枚举

### 2. 股票列表查询

实现 `list_stocks(filters: dict, page=1, page_size=50) -> dict`：

支持筛选字段：
- `market`（多选）
- `symbol` / `name` 文本搜索（匹配 `symbol`, `watchlist.name`, `watchlist.aliases_json`）
- `has_active_card`（bool）
- `pe_min`, `pe_max`
- `pct_chg_min`, `pct_chg_max`
- `volume_min`, `volume_max`
- `data_quality`（多选 `pe_status`）
- `recent_signal_days`（最近 N 天是否有触发信号）

返回字段（参考 `ui_design_phase1.md` §4.1）：
- `symbol`, `market`, `name`
- `latest_trade_date`
- `latest_close`
- `price_adj_factor`
- `pe_ttm`, `pe_status`
- `pct_chg`
- `active_card_id`
- `tier_state`（可选，第一期可留空）
- `signal_count_5d`
- `last_run_status`

### 3. 单股基础数据

实现 `get_stock_meta(symbol: str) -> dict`：
- `watchlist` 基本信息
- 最新 `daily_bars` 数据
- 最新 `indicators_daily` 数据
- 当前 active 卡片简要信息
- 最近 `pipeline_runs` 状态

实现 `get_stock_bars(symbol, granularity='daily', start, end, price='unadjusted') -> list[dict]`：
- `granularity='daily'`：从 `daily_bars` 取
- `granularity='weekly'`：从 `weekly_bars` 取
- `price`：
  - `unadjusted`：返回 `open_raw/high/low/close/volume`
  - `fully_adjusted`：价格乘以 `price_adj_factor`，成交量除以 `share_factor`
  - `adjusted_back`：保留原始价格，指标按原值返回（用于和均线叠图）

实现 `get_stock_indicators(symbol, granularity='daily', start, end, fields=None) -> list[dict]`：
- `fields=None` 返回全部指标
- 否则只返回指定字段
- 字段校验：只允许 `indicators_daily` / `indicators_weekly` 实际存在的列

### 4. 信号查询

实现 `list_signals(filters: dict, page=1, page_size=100) -> dict`：

支持筛选：
- `symbols`（多选）
- `signals`（多选 signal 类型）
- `states`（多选 state）
- `triggered`（bool）
- `start`, `end` 日期
- `anchor_id`

返回字段：
- `fact_id`, `symbol`, `observed_on`, `signal`, `state`, `anchor_id`, `triggered`, `active_until`, `details_json`

实现 `get_signal_details(fact_id: int) -> dict`：
- 返回单条 `signal_facts` 完整记录
- 同时返回关联 `weekly_anchors` 信息（若 `anchor_id` 非空）

### 5. 卡片查询

实现 `list_cards(filters: dict, page=1, page_size=50) -> dict`：
- 支持 `symbol`, `status`, `effective_from/to` 筛选
- 返回卡片摘要 + 版本链

实现 `get_card_detail(card_version_id: str) -> dict`：
- 返回完整卡片记录
- 解析 `price_tiers_json`, `invalidation_json`, `swing_box_json` 等 JSON 字段

### 6. 执行记录

实现 `list_executions(symbol=None, start=None, end=None, page=1, page_size=50) -> dict`：
- 从 `executions` 表查询
- 支持 symbol 和 executed_at 日期范围筛选

### 7. 运行记录

实现 `list_pipeline_runs(filters, page=1, page_size=50) -> dict`：
- 支持 `run_id`, `stage`, `status`, 时间范围筛选

实现 `list_report_runs(filters, page=1, page_size=50) -> dict`：
- 支持 `report_type`, `symbol`, `trade_date`, `status` 筛选

### 8. 辅助查询

实现：
- `get_trading_dates(market, start, end) -> list[str]`
- `get_latest_trade_date(market) -> str`
- `get_watchlist() -> list[dict]`
- `get_benchmark_bars(symbol, start, end) -> list[dict]`（用于单股图叠加基准指数）

### 9. 测试

在 `tests/test_ui_queries.py` 中：
- 对每个查询函数至少一个 happy path 测试
- 测试筛选条件组合
- 测试日期边界
- 测试 price 模式切换

## 验收标准

- [ ] `queries.py` 覆盖 `ui_design_phase1.md` §4 全部 API 所需数据。
- [ ] 所有查询均为只读，不修改数据库。
- [ ] 参数化 SQL，无字符串拼接。
- [ ] 单元测试通过：`pytest tests/test_ui_queries.py`。
- [ ] 返回数据能直接用于 `/stocks` 列表、`/stock/{symbol}` 图表、`/signals` 时间轴。

## 依赖

- [[task-00-init]]

## 后续任务

- [[task-02-layout]]
- [[task-03-stock-list]]
- [[task-04-stock-dashboard]]
