# 任务 05：纯指标分析页 `/indicators`

## 任务目标

实现不绑定具体股票的纯指标分析页，用户可筛选任意股票，选择任意日线/周线指标，进行多指标时间序列对比。

## 输入

- `docs/ui_design_phase1.md` §3.5 / §4.4
- `scripts/ui/queries.py` 中的 `get_stock_indicators`

## 输出

- `scripts/ui/templates/indicators.html`
- `scripts/ui/static/js/indicators.js`
- `GET /indicators` 路由
- `GET /api/indicators` 路由

## 详细步骤

### 1. 后端路由 `GET /api/indicators`

Query 参数：
- `symbols`：逗号分隔，最多 6 只
- `granularity`：daily / weekly
- `start`, `end`
- `fields`：逗号分隔的指标字段，最多 6 个
- `price`：unadjusted / fully_adjusted（用于价格列）

返回：

```json
{
  "symbols": [...],
  "granularity": "daily",
  "start": "2026-01-01",
  "end": "2026-08-07",
  "fields": ["ma5", "ma20", "rsi12"],
  "series": {
    "ma5": {
      "2026-08-07": {"603605.SH": 57.5, "002747.SZ": 23.1},
      ...
    }
  }
}
```

### 2. 页面布局

左侧：筛选区
- 股票多选（从 `watchlist` 搜索或从股票列表带入）
- 时间粒度切换
- 日期范围
- 价格口径
- 指标选择器（多选，最多 6 个）
- "添加子图" / "清空" 按钮

右侧/下方：图表区
- 每个指标一个 ECharts 子图
- 同一 X 轴联动
- 图例显示 symbol

### 3. 指标选择器

分组显示：
- 价格/成交量：close, volume, amount, pct_chg, amplitude
- 均线：ma5, ma10, ma20, ma60, ma120, ma250
- MACD：dif, dea, macd_hist
- RSI：rsi6, rsi12, rsi24
- BOLL：boll_mid, boll_upper, boll_lower, boll_bandwidth
- KDJ：kdj_k, kdj_d, kdj_j
- 量能：vol_ma5, vol_ma10, vol_mean20, vol_std20, vol_mean60, vol_std60
- 估值：pe_ttm

### 4. 图表展示

- 每个子图一个指标
- 若多个股票选择同一指标，用不同颜色折线叠加
- 标题显示指标名
- 鼠标悬停显示所有股票该日数值

### 5. 多股票处理

- 不同股票可能有不同交易日
- 后端返回按日期对齐的数据
- ECharts 用 category 类型 X 轴，缺失值自动断开

### 6. CSV 导出

提供"导出 CSV"按钮：
- 下载当前筛选条件下的数据
- CSV 列：`date, symbol, field1, field2, ...`

### 7. 子图管理

用户可以：
- 添加/删除子图
- 调整子图顺序
- 重置为默认 4 个指标

## 验收标准

- [ ] 用户可以选择 1~6 只股票和 1~6 个指标。
- [ ] 每个指标在一个独立子图展示，多个股票用不同颜色折线。
- [ ] 日线/周线可切换。
- [ ] 日期范围、价格口径可调。
- [ ] 支持 CSV 导出。
- [ ] 页面 URL 包含用户选择，刷新后可恢复。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]
- [[task-03-stock-list]]（可选，用于带入 symbol）

## 后续任务

- 无强依赖。
