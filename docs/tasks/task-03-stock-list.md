# 任务 03：股票列表页 `/stocks`

## 任务目标

实现股票列表页，支持多维度筛选、排序、分页和多选进入对比。

## 输入

- `docs/ui_design_phase1.md` §3.2 / §4.1
- `scripts/ui/queries.py` 中的 `list_stocks`

## 输出

- `scripts/ui/templates/stocks.html`
- `scripts/ui/static/js/stocks.js`
- `GET /stocks` 路由
- `GET /api/stocks` 路由

## 详细步骤

### 1. 后端路由 `GET /api/stocks`

在 `app.py` 中实现，从 query 参数读取筛选条件：

```
?market=CN&market=HK&q=珀莱雅&has_active_card=1&pe_min=10&pe_max=50&page=1&page_size=50&sort=latest_trade_date&order=desc
```

调用 `queries.list_stocks(filters, page, page_size, sort, order)` 返回 JSON。

### 2. 筛选面板

`stocks.html` 左侧或顶部放置筛选表单：

| 筛选项 | 类型 |
|--------|------|
| 市场 | 多选 checkbox |
| 搜索框 | 文本输入 |
| 有 active 卡片 | 单选（是/否/全部） |
| 最近 N 天有信号 | 数字输入 |
| PE(TTM) 范围 | 最小/最大 |
| 涨跌幅范围 | 最小/最大 |
| 成交量范围 | 最小/最大 |
| 数据质量 | 多选（OK / 降级 / 缺失） |
| 卡片状态 | 单选 |

"应用筛选"和"重置"按钮。

### 3. 列表表格

列与 `ui_design_phase1.md` §3.2 一致：

- 选择框
- Symbol
- 名称/市场
- 最新交易日
- 最新收盘价
- 复权因子
- PE(TTM) + `pe_status` 标签
- 今日涨跌幅
- 近 5 日信号数
- 档位状态
- 运行状态

行可点击跳转 `/stock/{symbol}`。

### 4. 排序

支持点击表头排序：
- `latest_trade_date`
- `latest_close`
- `pe_ttm`
- `pct_chg`
- `signal_count_5d`

### 5. 分页

底部显示：
- 总页数
- 上一页/下一页
- 每页条数选择（25/50/100）

### 6. 批量操作

选中多只股票后：
- 显示浮动操作栏
- "对比选中" 跳转 `/compare?symbols=...`
- "查看指标" 跳转 `/indicators?symbols=...`

### 7. 状态标签

- `pe_status` 不为空时显示警告色
- `last_run_status` = `failed` 时显示红色
- `last_run_status` = `degraded` 时显示黄色
- 停牌时整行标灰

### 8. 前端交互 `stocks.js`

实现：
- 筛选表单提交转换为 URL query
- 表格数据通过 `fetchJSON('/api/stocks' + query)` 加载
- 分页、排序重载
- 全选/取消全选
- 批量操作按钮启用/禁用

### 9. 性能

- 后端 SQL 只查询需要的列
- 默认 `page_size=50`
- 搜索框输入防抖 300ms

## 验收标准

- [ ] `/stocks` 页面能按市场、代码/名称、PE 范围、涨跌幅等筛选。
- [ ] 表头排序和分页工作正常。
- [ ] 选中多只股票后能跳转到对比页。
- [ ] 页面加载时间不慢于 1 秒（默认 50 条）。
- [ ] 筛选条件在 URL 中持久化，刷新不丢失。
- [ ] 数据异常/降级状态有视觉提示。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]

## 后续任务

- [[task-04-stock-dashboard]]
- [[task-07-compare]]
