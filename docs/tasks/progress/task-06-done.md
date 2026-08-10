# Task 06 完成记录

完成日期：2026-08-10

## 实现摘要

- `GET /api/signals`（symbols/signals/states/triggered/日期/anchor/排序/分页）、`GET /api/signals/{fact_id}`（附 weekly_anchors）。
- `templates/signals.html` + `static/js/signals.js`：股票/信号类型（distinct 动态）/状态（distinct）多选、是否触发、日期区间、anchor_id；表格（观测日/股票/信号/状态/触发/活跃截止/anchor/详情）；详情弹窗展示完整 details_json + 锚点信息 + run_id/rule_version/config_hash；按日期/按股票分组视图；ECharts 时间轴视图（Y=信号类型，颜色=状态，大小=触发与否）；跳转单股页定位日期；URL 同步；分页。

## 测试

`test_ui_api.py`：筛选组合、详情含锚点、日期边界、排序。

## 决策

- 信号类型/状态选项由 `SELECT DISTINCT` 动态加载，不写死枚举。
