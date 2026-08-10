# Task 11 完成记录

完成日期：2026-08-10

## 实现摘要

- `tests/test_ui_api.py` 37 项：全部 API 端到端（HTTP 200/JSON 结构/筛选生效）、参数校验（无效日期/排序/价格/粒度 400、超量 400）、口径一致性（fully_adjusted = raw×factor、unadjusted MA 折回）、页面路由 200 与 JS 可加载、报告文件服务与路径穿越拦截。
- 性能实测（真实库，本机）：`/api/stocks` 50 条 ~8ms、3 年日线 ~5ms、6×6 指标 ~6ms、6 股对比 ~3ms、首页 ~5ms——全部远低于验收线（1s/2s/3s/2s）。
- 手工端到端：启动 `uv run python -m scripts.ui.app`，curl 验证全部页面 200、API 返回正确、报告文件可直接打开、路径穿越 404。

## 修复清单（过程中关闭）

- 种子数据列数/绑定数错误、signal triggered NULL、指标存储应为复权值。
- 查询层：IN 筛选兼容字符串/列表、周线不复权聚合、价格刻度指标折回、card 排序列歧义、compare metric 读取与 close 走 bars 口径、dashboard alerts 缺 status 列。
- 数据质量虚拟码 ok/degraded/missing 兼容真实库 `ok;degraded_available_at`。

## 验收对照

- 全部 API 单元测试通过（262 项全绿）。
- 参数校验覆盖主要错误路径。
- 页面路由全部 200 且含关键元素；JS 全部可加载且 node --check 语法通过。
- 性能达标；浏览器端到端验证完成；修复清单全部关闭。
