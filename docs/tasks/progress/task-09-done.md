# Task 09 完成记录

完成日期：2026-08-10

## 实现摘要

- `GET /api/runs`（run_id/stage/status/起止时间筛选；duration_sec、config_hash_short）、`GET /api/reports`（type/symbol/trade_date/status 筛选）。
- `templates/runs.html` + `static/js/runs.js`：Pipeline/Report 双 tab、顶部统计看板（总数/成功/降级/失败/最近运行）、Pipeline 表（run_id/stage/data_cutoff/status/started/duration/rule_version/card/详情展开完整记录含 config_hash 前 8 位与错误）、Report 表（file_path 链接直接打开 reports/）、60s 自动刷新。
- 报告文件访问经 `GET /reports/<path>`（限定 `.md/.markdown`、禁止越出 reports 目录）。

## 测试

`test_ui_api.py`：runs/reports 筛选、duration 字段、报告文件服务与路径穿越拦截。

## 决策

- Report Runs 的 report_type/status 复用 Pipeline 筛选输入框（tab 切换解释不同）。
