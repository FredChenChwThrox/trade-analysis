# Task 03 完成记录

完成日期：2026-08-10

## 实现摘要

- `GET /api/stocks`（筛选/排序/分页，支持 market/q/has_active_card/pe 范围/pct_chg 范围/volume 范围/pe_status 多选含虚拟码 ok/degraded/missing/recent_signal_days）。
- `templates/stocks.html` + `static/js/stocks.js`：左侧筛选面板、可排序表格（symbol/名称/最新交易日/收盘/复权因子/PE+状态/涨跌幅/近5日信号/档位/运行状态）、全选、分页、每页条数、"对比选中"→/compare、"查看指标"→/indicators、URL 全状态同步、停牌行标灰、failed 红标。
- 数据质量虚拟码：`ok`（pe_status 空或 `ok%`）、`degraded`（含 degraded）、`missing`（其他原因码），兼容真实库 `ok;degraded_available_at` 标注。

## 测试

`tests/test_ui_queries.py` 中股票列表筛选组合（market/搜索/卡片/pe 范围/pct/volume/质量/信号天数/排序分页）；`test_ui_api.py` 覆盖 HTTP 层。

## 决策

- 数据质量三档按 pe_status 语义映射，而非精确匹配（真实库是 `;` 分隔标注）。
