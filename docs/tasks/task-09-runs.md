# 任务 09：运行状态页 `/runs`

## 任务目标

实现 `pipeline_runs` 和 `report_runs` 的运行状态展示页，帮助用户追踪数据更新、运行批次和报告生成情况。

## 输入

- `docs/ui_design_phase1.md` §3.8 / §4.8
- `scripts/ui/queries.py` 中的 `list_pipeline_runs`, `list_report_runs`

## 输出

- `scripts/ui/templates/runs.html`
- `scripts/ui/static/js/runs.js`
- `GET /runs` 路由
- `GET /api/runs` 路由
- `GET /api/reports` 路由

## 详细步骤

### 1. 后端路由

`GET /api/runs`：
- Query：`run_id`, `stage`, `status`, `start`, `end`, `page`, `page_size`, `sort`, `order`
- 返回 `pipeline_runs`

`GET /api/reports`：
- Query：`report_type`, `symbol`, `trade_date`, `status`, `page`, `page_size`
- 返回 `report_runs`

### 2. 页面布局

Tabs 切换：
- Pipeline Runs
- Report Runs

### 3. Pipeline Runs 列表

列：
- `run_id`
- `stage`
- `as_of`
- `data_cutoff`
- `status`（颜色标签）
- `started_at`
- `finished_at`
- `duration`
- `adapter_version`
- `rule_version`
- `card_version_id`（若涉及）
- `error`（截断显示，点击展开）

筛选：
- `run_id` 文本
- `stage` 下拉
- `status` 多选
- 起止时间

### 4. Report Runs 列表

列：
- `report_run_id`
- `report_type`
- `symbol`
- `trade_date`
- `revision`
- `status`
- `file_path`
- `card_version_id`
- `rule_version`
- `created_at`

筛选：
- `report_type`
- `symbol`
- `trade_date`
- `status`

### 5. 详情展开

- 点击行展开完整记录
- 显示 `config_hash` 前 8 位
- 显示 `input_snapshot_json` 摘要（若太大则截断）
- 若 `file_path` 存在且是 Markdown，提供"查看报告"链接（直接打开 `reports/` 下的文件，需配置 static 映射）

### 6. 统计看板

顶部显示：
- 今日运行总数
- 成功/降级/失败数量
- 最近成功运行时间
- 最近失败运行时间和错误

### 7. 状态颜色

- `success`：绿色
- `degraded`：黄色
- `failed`：红色
- `running`：蓝色
- `complete`：绿色
- `incomplete`：黄色

### 8. 前端 `runs.js`

- Tabs 切换
- 筛选、分页、排序
- 详情展开
- 统计看板
- 自动刷新（每 60 秒）

## 验收标准

- [ ] Pipeline Runs 和 Report Runs 都能列表展示。
- [ ] 支持按 run_id、stage、status、时间筛选。
- [ ] 状态颜色正确。
- [ ] 详情展开显示完整运行记录和错误信息。
- [ ] 报告文件路径可直接打开。
- [ ] 顶部统计看板实时。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]

## 后续任务

- 无。
