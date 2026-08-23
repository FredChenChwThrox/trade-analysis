# Task 02 完成记录

完成日期：2026-08-10

## 实现摘要

- `templates/base.html`：Tailwind CSS CDN + ECharts CDN + `app.css` + `common.js`，`{% block content %}`/`{% block scripts %}`。
- `partials/navbar.html`（首页/股票列表/指标分析/信号时间轴/多股对比/卡片列表/运行状态，当前页高亮）、`partials/filter_bar.html`（可折叠全局筛选：市场/搜索/日期区间/快捷按钮，URL 状态同步）、`partials/footer.html`（库路径/最后更新/Flask 版本）。
- `templates/404.html`、`500.html`；`static/css/app.css`（状态色、表格、卡片、图表高度、toast、json-view）；`static/js/common.js`（formatDate/formatNumber/buildQueryString/debounce/fetchJSON/showToast/renderStatusBadge/initDateRangePicker/initStockSearch/escapeHtml）。
- 后端：`GET /api/markets`、`GET /api/stocks/search`；context processor 注入 footer 信息；`/reports/<path>` 只读服务 reports/ Markdown（路径越界拦截）。

## 测试

`tests/test_ui_layout.py` 10 项：markets、search、静态资源、base 渲染、导航、404 页面/API、common.js 工具函数。

## 决策

- 页面用服务端渲染骨架 + JS 拉 API 渲染（第一期无构建链）。
- 错误页：`/api/` 返回 JSON，页面返回模板。
