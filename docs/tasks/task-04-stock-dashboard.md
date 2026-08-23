# 任务 04：单股综合仪表板 `/stock/{symbol}`

## 任务目标

实现单股综合仪表板，展示价格、指标、信号、卡片和执行记录，并支持用户自由调整显示内容。

## 输入

- `docs/ui_design_phase1.md` §3.3 / §4.2 / §4.3
- `scripts/ui/queries.py` 中的 `get_stock_meta`, `get_stock_bars`, `get_stock_indicators`

## 输出

- `scripts/ui/templates/stock.html`
- `scripts/ui/static/js/stock.js`
- `GET /stock/<symbol>` 路由
- `GET /api/stocks/<symbol>/bars` 路由
- `GET /api/stocks/<symbol>/indicators` 路由

## 详细步骤

### 1. 后端路由

`GET /stock/<symbol>`：
- 渲染 `stock.html`，传入 `symbol` 和 `ui_config`

`GET /api/stocks/<symbol>`：
- 返回 `get_stock_meta(symbol)` 的 JSON

`GET /api/stocks/<symbol>/bars`：
- Query 参数：
  - `granularity`：daily / weekly
  - `start`, `end`
  - `price`：unadjusted / fully_adjusted / adjusted_back
- 返回 OHLCV 时间序列

`GET /api/stocks/<symbol>/indicators`：
- Query 参数：
  - `granularity`：daily / weekly
  - `start`, `end`
  - `fields`：逗号分隔的字段名
- 返回指标时间序列

`GET /api/stocks/<symbol>/signals`：
- 返回最近 N 天信号
- Query 参数：`start`, `end`, `limit`

`GET /api/stocks/<symbol>/cards`：
- 返回 active 和历史卡片

`GET /api/stocks/<symbol>/executions`：
- 返回执行记录

### 2. 概览栏

页面顶部展示：
- 股票名称、市场、币种、基准指数
- 当前 active 卡片 `card_version_id`
- 最新 `close_raw`、`price_adj_factor`
- 最新 `pe_ttm`、`pe_status`
- 最新涨跌幅 `pct_chg`
- 当前档位位置（用 `price_tiers_json` 和 `close_raw` 计算）
- 当前数据截止 `as_of` / `run_id`

档位位置计算（前端或后端均可）：
- 现价落在哪一档（tier1/tier2/tier3）
- 若未进入任何一档，显示"距 tierX 上/下边界 Y%"

### 3. 主图区

使用 ECharts 实现：
- 主图：K 线或收盘价折线
  - 默认折线图（K 线图可选切换）
  - 价格口径切换：`unadjusted` / `fully_adjusted`
  - 叠加均线 MA5/10/20/60/120/250（可勾选）
  - 标记线：卡片价区、证伪线、箱体边界
- 副图区：最多 4 个可配置 panel
  - 成交量
  - MACD
  - RSI（6/12/24 三条线）
  - KDJ（K/D/J 三条线）
  - BOLL（中/上/下三条线）
  - PE(TTM)
  - 涨跌幅

每个 panel 独立 Y 轴，X 轴与主图联动缩放。

### 4. 控制面板

提供用户可调整的选项：

| 控制项 | 选项 |
|--------|------|
| 时间粒度 | 日线 / 周线 |
| 日期范围 | 开始 / 结束，或快捷按钮 |
| 价格口径 | 不复权 / 完全复权 |
| 主图类型 | 折线 / K线 |
| 叠加均线 | MA5/10/20/60/120/250 多选 |
| 副图指标 | 最多 4 个下拉选择 |
| 显示卡片标记 | 是/否 |
| 显示执行记录 | 是/否 |

### 5. 卡片标记

若开启"显示卡片标记"：
- 主图上用 markArea / markLine 标出价区
- 用水平线标出 `invalidation_json.line`
- 标出 `swing_box_json.box_low/box_high`
- 标出 `right_side_trigger_json` 关键位/止损位

### 6. 信号时间线

主图下方显示信号列表：
- 表格列：`observed_on`, `signal`, `state`, `triggered`, `active_until`
- 展开 `details_json`
- 在图表上对应日期添加 scatter 标记（可选开关）

### 7. 卡片与执行

页面右侧/下方可折叠：
- 当前 active 卡片 JSON 格式化展示
- 历史版本列表
- 执行记录表格

### 8. 复权口径说明

在价格图下方明确显示当前价格口径：
- "当前显示：不复权价格；指标已按当日复权因子折回"
- 或 "当前显示：完全复权价格；指标为原始复权值"

### 9. 前端 `stock.js`

实现：
- `initPriceChart(symbol, config)`
- `initIndicatorPanel(containerId, field, data)`
- `updateAllCharts()` 在配置变化时重载
- 用 `URLSearchParams` 同步用户选择到 URL，便于分享

## 验收标准

- [ ] 单股页能展示价格、PE、成交量、MACD、RSI、KDJ、BOLL 等指标时间序列。
- [ ] 用户可以切换日线/周线、价格口径、日期范围、叠加均线和副图指标。
- [ ] 当前 active 卡片的价区/证伪线/箱体在主图中可视化。
- [ ] 信号时间线能展示最近触发信号，并展开 `details_json`。
- [ ] 所有图表联动缩放，鼠标悬停显示日期和数值。
- [ ] 页面 URL 包含用户选择，刷新后状态可恢复。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]

## 后续任务

- [[task-05-indicators]]
- [[task-06-signals]]
