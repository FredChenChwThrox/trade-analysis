# 任务 11：测试与联调

## 任务目标

对 UI 各模块进行测试和联调，确保只读查询正确、页面交互稳定、性能达标。

## 输入

- 已完成的前序任务代码
- `data/market.db`
- `tests/` 目录

## 输出

- `tests/test_ui_api.py`
- `tests/test_ui_queries.py`（若 task-01 未完成）
- 性能测试记录
- 修复清单

## 详细步骤

### 1. 单元测试

在 `tests/test_ui_api.py` 中测试每个 API：

- `GET /health`
- `GET /api/stocks`（各种筛选条件）
- `GET /api/stocks/{symbol}`
- `GET /api/stocks/{symbol}/bars`（daily/weekly，三种 price 模式）
- `GET /api/stocks/{symbol}/indicators`（各种字段组合）
- `GET /api/signals`
- `GET /api/indicators`
- `GET /api/compare`
- `GET /api/cards`
- `GET /api/runs`
- `GET /api/reports`
- `GET /api/dashboard`

每个测试验证：
- HTTP 200
- 返回 JSON 结构正确
- 关键字段存在
- 筛选条件生效

### 2. 参数校验测试

- 无效 symbol：返回 404 或空结果
- 无效日期格式：返回 400
- 无效字段名：返回 400
- 超出最大股票数/指标数：返回 400
- 过大的日期范围：返回 400

### 3. 口径一致性测试

- `price=unadjusted` 时，均线应折回
- `price=fully_adjusted` 时，价格应为 `close_raw * price_adj_factor`
- 周线数据只包含完成周
- 指标字段名与数据库一致

### 4. 页面测试

- 使用 Flask `test_client` 访问每个页面路由
- 验证 HTML 中关键元素存在
- 验证 JS 文件可加载

### 5. 性能测试

使用 Python 或 curl 测量：
- `/stocks` 默认加载时间 < 1s
- `/stock/{symbol}` 3 年日线数据加载 < 2s
- `/indicators` 6 只股票 6 个指标加载 < 3s
- `/compare` 6 只股票加载 < 2s

若超时，记录慢查询并在 `queries.py` 优化索引或分页。

### 6. 端到端验证

- 启动 `python -m scripts.ui.app`
- 浏览器/ curl 访问全部页面
- 验证图表正常渲染
- 验证筛选、分页、排序

### 7. 修复与回归

- 记录所有问题
- 修复后重新运行测试
- 确保不破坏前序任务功能

### 8. 文档更新

- 在 `docs/tasks/progress/` 记录测试结果
- 更新 `docs/ui_design_phase1.md` 若发现设计变更
- 在 `README.md` 或等效文件中添加启动说明

## 验收标准

- [ ] 所有 API 单元测试通过。
- [ ] 参数校验测试覆盖主要错误路径。
- [ ] 页面路由返回 200 且包含关键元素。
- [ ] 性能测试达标。
- [ ] 浏览器端到端验证完成。
- [ ] 修复清单中所有问题关闭。

## 依赖

- 除本任务外的所有前序任务。

## 后续任务

- 无。本任务为第一期末尾任务。
