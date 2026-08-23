# 任务 06：信号时间轴页 `/signals`

## 任务目标

实现以信号为中心的筛选和时间轴展示页，支持按股票、信号类型、状态、日期等条件筛选，并能展开查看 `details_json`。

## 输入

- `docs/ui_design_phase1.md` §3.6 / §4.5
- `scripts/ui/queries.py` 中的 `list_signals`

## 输出

- `scripts/ui/templates/signals.html`
- `scripts/ui/static/js/signals.js`
- `GET /signals` 路由
- `GET /api/signals` 路由

## 详细步骤

### 1. 后端路由 `GET /api/signals`

Query 参数：
- `symbols`：逗号分隔，最多 20 只
- `signals`：逗号分隔的 signal 类型
- `states`：逗号分隔的 state
- `triggered`：0 / 1
- `start`, `end`
- `anchor_id`
- `page`, `page_size`
- `sort`, `order`

调用 `queries.list_signals(filters)` 返回 JSON。

### 2. 筛选面板

- 股票多选（搜索）
- 信号类型多选（从 `signal` 枚举动态加载，可调用 `SELECT DISTINCT signal FROM signal_facts`）
- 状态多选（state 枚举）
- 是否触发：全部 / 仅触发
- 日期范围
- `anchor_id` 输入框
- 重置/应用按钮

### 3. 列表展示

表格列：
- `observed_on`
- `symbol`
- `signal`
- `state`
- `triggered`（绿色对勾/灰色横线）
- `active_until`
- `anchor_id`
- 操作：展开详情 / 查看单股

### 4. 详情展开

点击"展开详情"显示：
- 完整 `details_json` 格式化 JSON
- 关联的 `weekly_anchors` 信息
- `run_id`, `rule_version`, `config_hash`
- 点击查看该 signal 对应日期的单股图：`/stock/{symbol}?start=...&end=...&signal_highlight={fact_id}`

### 5. 时间轴视图（可选增强）

除表格外，提供"时间轴视图"切换：
- 横轴：时间
- 纵轴：signal 类型
- 每个点代表一个信号记录
- 颜色表示 state
- 大小表示 materiality（若可用）

### 6. 分组视图

提供两种分组方式：
- 按日期分组：看某一天发生了哪些信号
- 按股票分组：看某只股票在一段时间内的信号序列

### 7. 状态标签

- `triggered=1`：绿色
- `state=active`：蓝色
- `state=inactive`：灰色
- `state=incomplete`：黄色
- `state=triggered`：绿色
- `state=failed`：红色

### 8. 前端 `signals.js`

- 筛选表单提交
- 表格渲染与分页
- 详情展开/收起
- 分组视图切换
- URL 状态同步

## 验收标准

- [ ] 能按股票、信号类型、状态、是否触发、日期范围筛选。
- [ ] 表格列出所有匹配信号，并正确分页。
- [ ] 点击"展开详情"显示格式化 `details_json`。
- [ ] 能从信号行跳转单股页并定位到对应日期。
- [ ] 状态颜色标签正确。
- [ ] URL 包含筛选条件，刷新后可恢复。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]
- [[task-04-stock-dashboard]]（跳转用）
