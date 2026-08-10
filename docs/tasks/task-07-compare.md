# 任务 07：多股对比页 `/compare`

## 任务目标

实现多股对比页，用户选择 2~6 只股票和 1 个指标，查看同一时间序列下的对比折线图。

## 输入

- `docs/ui_design_phase1.md` §3.4 / §4.7
- `scripts/ui/queries.py` 中的指标查询能力

## 输出

- `scripts/ui/templates/compare.html`
- `scripts/ui/static/js/compare.js`
- `GET /compare` 路由
- `GET /api/compare` 路由

## 详细步骤

### 1. 后端路由 `GET /api/compare`

Query 参数：
- `symbols`：逗号分隔，2~6 只
- `metric`：单个指标字段名
- `granularity`：daily / weekly
- `start`, `end`
- `price`：unadjusted / fully_adjusted

返回：

```json
{
  "symbols": ["603605.SH", "002747.SZ"],
  "metric": "pe_ttm",
  "granularity": "daily",
  "dates": ["2026-01-04", "2026-01-05", ...],
  "series": {
    "603605.SH": [23.1, 23.5, ...],
    "002747.SZ": [45.2, 44.8, ...]
  },
  "metadata": {
    "603605.SH": {"name": "珀莱雅", "market": "CN"},
    "002747.SZ": {"name": "埃斯顿", "market": "CN"}
  }
}
```

### 2. 页面布局

顶部：
- symbol 添加/删除输入框
- 指标选择下拉
- 时间粒度切换
- 日期范围
- 价格口径

中部：
- 大尺寸 ECharts 折线图

底部：
- 并排表格：每只股票最新值、起始值、区间涨跌幅、最大值、最小值、均值

### 3. 股票选择

- 输入框支持 symbol 和名称搜索
- 已选股票显示为 tag，可删除
- 最多 6 个
- 最少 2 个

### 4. 指标选择

下拉框列出所有可选指标：
- close
- volume
- pe_ttm
- pct_chg
- ma5, ma10, ma20, ma60, ma120, ma250
- dif, dea, macd_hist
- rsi6, rsi12, rsi24
- boll_mid, boll_upper, boll_lower
- kdj_k, kdj_d, kdj_j
- vol_ma5, vol_ma10 等

### 5. 图表

- 多条折线
- 图例可点击隐藏/显示
- 工具栏：数据缩放、保存图片
- 鼠标悬停显示所有股票该日数值
- 开启"标准化"开关，把各股票值归一化到同一起点（便于比较走势而非绝对值）

### 6. 统计表格

对选中的区间计算：
- 最新值
- 区间起始值
- 区间涨跌幅（%）
- 最大值 / 最小值
- 平均值
- 标准差

### 7. 价格相关指标处理

- `price=unadjusted`：`close`, `ma*` 等价格类指标用不复权值
- `price=fully_adjusted`：用完全复权值
- 对于非价格指标（RSI、MACD、PE 等），价格参数无效，可忽略

### 8. 前端 `compare.js`

- 股票 tag 管理
- 指标选择联动
- 图表加载
- 统计表格计算
- URL 状态同步

## 验收标准

- [ ] 支持 2~6 只股票、1 个指标同一时间序列对比。
- [ ] 日线/周线可切换。
- [ ] 提供标准化切换，便于比较走势。
- [ ] 底部统计表格显示区间统计。
- [ ] 支持 URL 参数，从股票列表页选中后直接跳转。
- [ ] 鼠标悬停显示所有股票同一日期数值。

## 依赖

- [[task-01-queries]]
- [[task-02-layout]]
- [[task-03-stock-list]]（带入 symbol）

## 后续任务

- 无。
