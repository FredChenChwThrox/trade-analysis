# 任务 10：首页 `/`

## 任务目标

实现首页仪表板，汇总整库状态、运行情况和异常提示，作为用户进入系统的第一个视图。

## 输入

- `docs/ui_design_phase1.md` §3.1
- `scripts/ui/queries.py` 中的聚合查询

## 输出

- `scripts/ui/templates/index.html`
- `scripts/ui/static/js/index.js`
- `GET /` 路由
- `GET /api/dashboard` 路由

## 详细步骤

### 1. 后端路由 `GET /api/dashboard`

返回：

```json
{
  "markets": ["CN", "HK"],
  "total_stocks": 6,
  "stocks_with_data_today": 6,
  "stocks_with_active_card": 4,
  "stocks_with_signal_today": 3,
  "latest_trade_date": "2026-08-07",
  "latest_run": {
    "run_id": "daily_2026-08-07",
    "status": "success",
    "finished_at": "2026-08-07T18:00:00Z"
  },
  "run_stats": {
    "success": 120,
    "degraded": 5,
    "failed": 1
  },
  "alerts": [
    {
      "symbol": "601318.SH",
      "type": "data_incomplete",
      "message": "PE(TTM) 缺失：股本数据不可用"
    }
  ]
}
```

### 2. 状态卡片区

顶部 4 个卡片：
- 股票总数
- 今日有数据数
- active 卡片数
- 今日触发信号数

每个卡片点击可进入对应列表页。

### 3. 运行状态区

- 最近 7 天 `pipeline_runs` 状态趋势（按天聚合 success/degraded/failed 数量）
- 最近 24 小时运行列表（前 10 条）

### 4. 异常清单

列出需要关注的情况：
- `pipeline_runs.status = failed`
- `signal_facts.state = incomplete`
- `indicators_daily.pe_status` 不为空
- `daily_bars.trading_status = suspended`
- 最新卡片 `next_review_at` 到期

每行显示：
- symbol / 名称
- 异常类型
- 简短说明
- 查看链接

### 5. 最近交易日期

按市场展示：
- 市场名
- 最新交易日
- 下一个交易日

### 6. 快速入口

- 查看全池日报（跳转到 `/stocks`）
- 查看指标分析（跳转到 `/indicators`）
- 查看信号时间轴（跳转到 `/signals`）
- 查看运行状态（跳转到 `/runs`）

### 7. 前端 `index.js`

- 加载 `/api/dashboard`
- 渲染状态卡片
- 渲染运行趋势图
- 渲染异常清单
- 自动刷新（每 60 秒）

### 8. 后端聚合查询

在 `queries.py` 实现：
- `get_dashboard_summary() -> dict`
- `get_dashboard_alerts() -> list`
- `get_run_stats(days=7) -> list`

## 验收标准

- [ ] 首页展示股票总数、今日数据、active 卡片、今日信号。
- [ ] 运行状态趋势图显示最近 7 天。
- [ ] 异常清单列出失败/降级/incomplete/停牌/复核到期项。
- [ ] 每个异常项有查看链接。
- [ ] 页面 60 秒自动刷新。
- [ ] 首页加载时间不超过 2 秒。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]

## 后续任务

- 无。
