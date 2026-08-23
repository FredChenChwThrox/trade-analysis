# 任务 08：卡片列表页 `/cards`

## 任务目标

实现排期卡版本列表和详情页，支持按股票、状态、生效区间筛选，展示版本链和 JSON 内容。

## 输入

- `docs/ui_design_phase1.md` §3.7 / §4.6
- `scripts/ui/queries.py` 中的 `list_cards`, `get_card_detail`

## 输出

- `scripts/ui/templates/cards.html`
- `scripts/ui/static/js/cards.js`
- `GET /cards` 路由
- `GET /api/cards` 路由

## 详细步骤

### 1. 后端路由 `GET /api/cards`

Query 参数：
- `symbol`
- `status`：active / draft / superseded / rejected
- `effective_from`, `effective_to`
- `page`, `page_size`
- `sort`, `order`

调用 `queries.list_cards(filters)`。

### 2. 筛选面板

- symbol 搜索
- 状态单选/多选
- 生效区间起止
- 是否只看 active

### 3. 列表表格

列：
- `card_version_id`
- `symbol`
- 股票名称
- `status`
- `effective_from`
- `effective_to`
- `supersedes_id`
- 价区摘要（从 `price_tiers_json` 提取 tier1/2/3 低/高边界）
- `next_review_at`
- 操作：查看详情 / 查看单股

### 4. 卡片详情弹窗/展开

显示完整卡片字段：
- 基本信息
- `earnings_scenarios_json` 格式化
- `valuation_scenarios_json` 格式化
- `price_tiers_json` 用表格展示
- `invalidation_json` 展示证伪线
- `swing_box_json` 展示箱体
- `right_side_trigger_json` 展示右侧触发位
- `input_snapshot_json` 格式化
- 版本链（递归展示 `supersedes_id` 链）

### 5. 版本链展示

- 当前卡片 + 被替代卡片链
- 用时间线或树形展示
- 标注每个版本的 `effective_from/to` 和 `supersedes_id`

### 6. 价区可视化（可选）

在详情中展示：
- 三档价区用色块标注
- 证伪线用红色线
- 箱体用绿色框

### 7. 状态颜色

- `active`：绿色
- `draft`：黄色
- `superseded`：灰色
- `rejected`：红色

### 8. 前端 `cards.js`

- 筛选、分页
- 详情弹窗
- 版本链渲染
- 价区表格生成

## 验收标准

- [ ] 能按 symbol、状态、生效区间筛选卡片。
- [ ] 列表展示价区摘要和版本链关系。
- [ ] 详情页格式化展示所有 JSON 字段。
- [ ] 版本链可追溯至更早版本。
- [ ] 状态颜色正确。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]

## 后续任务

- 无。
