# 任务 02：基础模板与全局布局

## 任务目标

创建前端基础模板、全局样式和通用组件，为所有页面提供一致的布局、导航和筛选条。

## 输入

- `docs/ui_design_phase1.md` §3
- `config/ui.yaml`

## 输出

- `scripts/ui/templates/base.html`
- `scripts/ui/static/css/app.css`
- `scripts/ui/static/js/common.js`
- `scripts/ui/templates/partials/navbar.html`
- `scripts/ui/templates/partials/filter_bar.html`
- `scripts/ui/templates/partials/footer.html`

## 详细步骤

### 1. 基础模板 `base.html`

使用 Jinja2 继承结构：
- `<head>` 引入 Tailwind CSS CDN
- 引入 ECharts CDN
- 引入 `app.css`
- 定义 `{% block content %}`
- 引入 `common.js`

### 2. 导航栏 `navbar.html`

链接：
- 首页 `/`
- 股票列表 `/stocks`
- 指标分析 `/indicators`
- 信号时间轴 `/signals`
- 卡片列表 `/cards`
- 运行状态 `/runs`

当前页面高亮。

### 3. 全局筛选条 `filter_bar.html`

可折叠，包含：
- 市场选择（从 API `/api/markets` 加载）
- 股票搜索（symbol / name，带自动补全）
- 日期区间（开始/结束）
- active 卡片开关
- 快速按钮：今天、近 30 天、近 90 天、近 1 年

筛选条应能用于任意页面，通过 URL query 参数保持状态。

### 4. 页脚 `footer.html`

显示：
- 数据库最后更新时间
- 当前 `market.db` 路径
- 当前 Flask 版本

### 5. 全局样式 `app.css`

- 定义状态颜色：
  - `success` / `degraded` / `failed` / `incomplete` / `suspended`
- 表格样式
- 卡片容器样式
- 图表容器最小高度
- 响应式布局断点

### 6. 通用 JS `common.js`

实现：
- `formatDate(date)`
- `formatNumber(num, precision=2)`
- `buildQueryString(params)`
- `debounce(fn, ms)`
- `fetchJSON(url)` 封装
- `showToast(message, type)`
- `renderStatusBadge(status)`
- `initDateRangePicker(startId, endId)`
- `initStockSearch(inputId, onSelect)`

### 7. 后端辅助路由

在 `app.py` 添加：
- `GET /api/markets`：返回市场列表
- `GET /api/stocks/search?q=...`：返回匹配的股票（用于自动补全）

### 8. 错误页面

- `templates/404.html`
- `templates/500.html`

## 验收标准

- [ ] 所有页面继承 `base.html`。
- [ ] 导航栏在所有页面可用，当前页面高亮正确。
- [ ] 筛选条能在 URL 中保持状态，刷新页面后筛选条件不丢失。
- [ ] 股票搜索自动补全能正常返回 `watchlist` 数据。
- [ ] `common.js` 中的工具函数被后续任务复用。

## 依赖

- [[task-00-init]]
- [[task-01-queries]]

## 后续任务

- [[task-03-stock-list]]
- [[task-04-stock-dashboard]]
