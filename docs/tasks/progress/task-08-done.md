# Task 08 完成记录

完成日期：2026-08-10

## 实现摘要

- `GET /api/cards`（symbol/status/生效区间重叠筛选，价区摘要 tier_summary）、`GET /api/cards/{id}`（JSON 字段解析 + supersedes 版本链）。
- `templates/cards.html` + `static/js/cards.js`：symbol 搜索、状态多选、生效区间、列表表格（version_id/股票/状态/生效起止/替代/价区摘要/下次复核/详情）；详情弹窗：基本信息 + 版本链 + 价区表 + 全部 JSON 字段格式化（earnings/valuation/invalidation/swing_box/right_side/input_snapshot）。

## 测试

`test_ui_api.py`：active 筛选、详情 JSON、版本链；`test_ui_queries.py`：生效区间重叠。

## 决策

- 生效区间重叠筛选只针对有 effective_from 的卡片（draft 无生效日不计入）。
