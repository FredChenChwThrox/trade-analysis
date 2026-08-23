# Task 04 完成记录

完成日期：2026-08-10

## 实现摘要

- 页面 `/stock/{symbol}` + API：`/api/stocks/{symbol}`（meta）、`/bars`（daily/weekly × unadjusted/fully_adjusted/adjusted_back）、`/indicators`（字段白名单 + 折回）、`/signals`、`/cards`、`/executions`。
- `templates/stock.html`：概览栏（名称/市场/币种/基准/active 卡/收盘/因子/PE+状态/涨跌幅/档位/数据截止+run_id）、控制面板（粒度/价格口径/主图折线↔K线/日期+快捷/MA 勾选/卡片标记/执行标记）、主图、4 个副图 panel（成交量/MACD/RSI/KDJ/BOLL/PE/涨跌幅可选）、信号时间线表格、卡片 JSON 详情、执行记录表、价格口径说明行。
- `static/js/stock.js`：ECharts 主图（折线/K线、MA 叠加、卡片价区 markArea + 证伪线/箱体/右侧触发位 markLine、执行与信号 scatter），副图独立 Y 轴，echarts.connect 组联动缩放，URL 参数（granularity/price/chart/start/end/ma/panels/cards/exec）刷新恢复。
- 口径纪律：主图与指标同 price 模式请求，不复权时后端已折回 MA/BOLL，价格轴与卡片价区可直接对比（§5.1）。

## 测试

`test_ui_api.py`：meta/bars 三模式/indicators 折回与字段校验/signals/cards/executions；页面路由 200 与 JS 可加载。

## 决策

- 副图默认 volume/macd/rsi/kdj 四个；周线不复权在查询层按周边界聚合 daily_bars 原始值。
