# Task 01 完成记录

完成日期：2026-08-10

## 实现摘要

`scripts/ui/queries.py` 全部只读查询（docs/ui_design_phase1.md §4，task-01）：

- 基础：`list_markets`、`list_run_ids`、`list_card_status`、`get_watchlist`、`search_stocks`。
- 股票列表：`list_stocks(filters, page, page_size, sort, order)` 支持 market/q/has_active_card/pe 范围/pct_chg 范围/volume 范围/pe_status 多选/最近 N 天有信号；返回最新交易日/收盘/复权因子/PE/涨跌幅/active 卡/signal_count_5d/最近运行状态；`compute_tier_state` 计算档位（现价落入某档或距最近边界 %）。
- 单股：`get_stock_meta`（watchlist + 最新 bar + 最新指标 + active 卡 + 最近运行）、`get_stock_bars`（日线三种价格模式；周线完全复权取 weekly_bars、不复权按周边界聚合 daily_bars 原始值）、`get_stock_indicators`（字段白名单校验，unadjusted/adjusted_back 对价格刻度指标 ma*/boll* ÷ 当日因子折回 §5.1）。
- 信号：`list_signals`（symbols/signals/states/triggered/日期/anchor 筛选）、`get_signal_details`（附 weekly_anchors）。
- 卡片：`list_cards`（symbol/status/生效区间重叠筛选，价区摘要）、`get_card_detail`（JSON 字段解析 + supersedes 版本链）。
- 执行/运行：`list_executions`、`list_pipeline_runs`（duration/config_hash 前 8 位）、`list_report_runs`。
- 辅助：`get_trading_dates`、`get_latest_trade_date`、`get_benchmark_bars`。

关键实现决策：

- 全部参数化 SQL；排序字段白名单防注入；查询函数第一个参数为连接（测试注入临时库）。
- 周线不复权 = 用 weekly_bars 的周边界对 daily_bars 原始 OHLC 聚合（与 pipeline/weekly.py 的不复权口径一致），完全复权直接用 weekly_bars 存储值。
- 指标折回仅作用于价格刻度字段（MA/BOLL），RSI/KDJ/MACD/PE 等无量纲字段不折回。

## 测试

`tests/test_ui_queries.py` 50 项：每个查询函数 happy path + 筛选组合 + 日期边界 + 价格模式切换（折回/完全复权）+ 字段校验 + 排序分页。

## 问题与决策

- 指标行返回键用 `date`（前端时间序列一致），bar 行用 `trade_date`。
- IN 筛选参数兼容字符串与列表两种形态。
- 生效区间筛选只针对有 effective_from 的卡片（draft 无生效日不计入）。
