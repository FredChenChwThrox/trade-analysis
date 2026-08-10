# 第一期 UI 设计：基于数据库的股票分析与指标查看

## 1. 设计目标

- **只读展示**：第一期不写入数据库、不修改卡片、不执行交易。
- **不固定股票**：用户通过筛选条件从 `watchlist` 和数据库记录中动态选择股票。
- **时间序列为主**：每个指标必须能按时间变化展示，支持日线/周线切换。
- **筛选与调整**：支持按股票属性、日期区间、信号类型、指标阈值、运行状态等多维度筛选。
- **可追溯**：展示数据来源、规则版本、运行批次（`run_id` / `rule_version` / `config_hash`）。

## 2. 技术栈

| 层级 | 推荐选型 | 说明 |
|------|----------|------|
| 后端 | **Flask**（或 FastAPI）+ 原生 `sqlite3` | 项目已有 Python 环境，依赖轻量；`pyproject.toml` 中不需要新增重型依赖 |
| 前端 | 原生 HTML + **Tailwind CSS CDN** + **ECharts**（或 Plotly.js CDN） | 不引入构建链；折线图/多子图/时间轴组件成熟 |
| 数据访问 | `scripts/ui/queries.py` | 集中管理 SQL，复用 `scripts/pipeline/db.py` 的数据库路径 |
| 应用入口 | `scripts/ui/app.py` | 路由与 API |
| 模板 | `scripts/ui/templates/*.html` | 服务端渲染，避免复杂状态管理 |
| 配置 | `config/ui.yaml` | UI 默认筛选、分页、默认日期范围等可配置 |

**不推荐的选项**：
- 不引入 React/Vue/Next.js 构建链（超出第一期范围）。
- 不在第一期接实时行情或 WebSocket。

## 3. 页面结构

### 全局布局

- 顶部导航栏：首页 / 股票列表 / 指标对比 / 信号时间轴 / 运行状态 / 卡片列表
- 全局筛选条（所有页面可折叠）：
  - 市场：CN / HK
  - 股票搜索（按 symbol / name / 别名）
  - 日期区间选择
  - 运行批次 `run_id` 选择
  - 卡片状态：active / draft / superseded / all
- 页脚：数据来源、`market.db` 最后更新时间、当前 `git_commit` 或 `app_version`

### 3.1 首页 `/`

展示整库概览。

| 模块 | 内容 | 数据来源 |
|------|------|----------|
| 运行状态卡片 | 最新 `pipeline_runs` 状态（成功/降级/失败），各阶段数量 | `pipeline_runs` |
| 股票池概览 | 总股票数、今日有数据数、有 active 卡片数、今日触发信号数 | `watchlist`, `daily_bars`, `strategy_card_versions`, `signal_facts` |
| 最近交易日 | 各市场最新一个交易日 | `trading_calendar`, `daily_bars` |
| 异常清单 | 数据不完整、`incomplete/degraded` 的股票列表 | `signal_facts`, `pipeline_runs` |

### 3.2 股票列表 `/stocks`

核心入口，支持多维度筛选。

**筛选条件**（前端表单 + URL query 参数）：

| 字段 | 类型 | 对应表/字段 |
|------|------|-------------|
| 市场 | 多选 | `watchlist.market` |
| 股票代码/名称 | 文本搜索 | `watchlist.symbol`, `watchlist.name`, `watchlist.aliases_json` |
| 是否有 active 卡片 | 单选 | `strategy_card_versions.status` |
| 最近 N 天是否有信号 | 单选/数字 | `signal_facts` |
| 最新 PE(TTM) 范围 | 数值区间 | `indicators_daily.pe_ttm` 最新值 |
| 最新涨跌幅范围 | 数值区间 | `indicators_daily.pct_chg` |
| 最新成交量范围 | 数值区间 | `daily_bars.volume_raw` |
| 数据质量 | 多选 | `indicators_daily.pe_status`, `daily_bars.trading_status` |
| 是否有异常 | 单选 | `signal_facts.state = 'incomplete' / 'degraded'` |

**列表列**：

| 列 | 说明 |
|----|------|
| 选择框 | 用于批量进入对比页 |
| Symbol | 链接到单股页 |
| 名称/市场 | `watchlist.name` |
| 最新交易日 | `daily_bars.trade_date` |
| 最新收盘价 | `close_raw` |
| 复权因子 | `price_adj_factor` |
| PE(TTM) | 最新 `pe_ttm` + `pe_status` 标记 |
| 今日涨跌幅 | `pct_chg` |
| 信号数 | 最近 5 个交易日 `triggered = 1` 的信号数量 |
| 档位状态 | 当前 active 卡片价区与现价位置（由后端计算） |
| 运行状态 | 最新 `pipeline_runs` 状态 |

**操作**：
- 点击 symbol 进入单股页。
- 多选后点击"对比选中"进入多股对比页。

### 3.3 单股综合仪表板 `/stock/{symbol}`

**URL 参数**：`?start=YYYY-MM-DD&end=YYYY-MM-DD&indicators=ma,rsi,pe&price=adjusted`

左侧/顶部：股票信息 + 全局筛选条。

主体分区：

#### A. 概览栏

| 内容 | 来源 |
|------|------|
| 名称、市场、币种、基准指数 | `watchlist` |
| 当前 active 卡片版本 | `strategy_card_versions` |
| 最新收盘价、复权价、PE(TTM)、涨跌幅 | `daily_bars` + `indicators_daily` |
| 当前所处档位、距下边界百分比 | 后端用 `price_tiers_json` 与 `close_raw` 计算 |
| 最新运行 `run_id` 和 `as_of` | `pipeline_runs` |

#### B. 主图区

- 主图：K 线或收盘价折线
  - 价格口径切换按钮：**不复权 / 复权折回 / 完全复权**
  - 均线叠加：MA5/10/20/60/120/250（可勾选）
  - 标记：卡片价区、证伪线、箱体边界、买点/卖点/执行记录
- 副图（多 panel，可拖拽调整）：
  - 成交量 + 均量
  - MACD
  - RSI（6/12/24）
  - KDJ
  - BOLL 带宽
  - PE(TTM)

每个 panel 独立 Y 轴，X 轴同步缩放。

#### C. 信号时间线

- 时间轴表格：最近 30/60/90 天 `signal_facts`
- 列：`observed_on`, `signal`, `state`, `triggered`, `active_until`, `anchor_id`
- 展开 `details_json` 显示阈值、原值、原因码

#### D. 卡片与执行

- 当前 active 卡片：价区、证伪线、箱体、右侧触发位 JSON 格式化展示
- 历史卡片版本列表（可折叠）
- 执行记录列表

### 3.4 多股对比页 `/compare`

**URL 参数**：`?symbols=603605.SH,002747.SZ&metric=pe_ttm&start=...&end=...`

- 输入框动态添加 symbol（从 `watchlist` 搜索）
- 选择对比指标：收盘价、PE(TTM)、RSI12、成交量、涨跌幅、MACD 柱等
- 显示同一 X 轴下的多条折线
- 底部表格：关键指标最新值并排

### 3.5 纯指标分析页 `/indicators`

不绑定具体股票，从筛选结果中选择 1~N 只股票，选择任意日线/周线指标，进行时间序列查看。

**筛选表单**：
- 股票（多选，从股票列表页带入或搜索）
- 时间粒度：日线 / 周线
- 开始/结束日期
- 复权口径：收盘价、复权收盘价
- 指标选择器（多选）：
  - 价格/成交量：close, volume, amount, pct_chg, amplitude
  - 均线：ma5, ma10, ma20, ma60, ma120, ma250
  - MACD：dif, dea, macd_hist
  - RSI：rsi6, rsi12, rsi24
  - BOLL：boll_mid, boll_upper, boll_lower, boll_bandwidth
  - KDJ：kdj_k, kdj_d, kdj_j
  - 量能：vol_ma5, vol_ma10, vol_mean20, vol_std20, vol_mean60, vol_std60
  - 估值：pe_ttm

**展示**：
- 默认每个指标一个子图，最多 6 个子图
- 支持导出 CSV

### 3.6 信号筛选页 `/signals`

以信号为中心的筛选视图。

**筛选条件**：
- `symbol`（多选/搜索）
- `signal` 类型（多选）
- `state`（多选）
- `triggered` 是否触发
- `observed_on` 日期区间
- `anchor_id`
- `active_until` 区间

**展示**：
- 表格列出所有匹配 `signal_facts`
- 可按 `symbol` 或 `observed_on` 分组
- 展开 `details_json`
- 点击 symbol 跳转单股页并定位到对应日期

### 3.7 卡片列表页 `/cards`

- 筛选：`symbol`, `status`, `effective_from/to` 区间
- 列表列：version id、status、symbol、生效区间、价区摘要、生成 `run_id`
- 点击展开完整 JSON
- 版本链展示（`supersedes_id` 链）

### 3.8 运行状态页 `/runs`

- `pipeline_runs` 列表
- 筛选：`run_id`, `stage`, `status`, `symbol`
- 展示各阶段起止时间、版本三元组、错误信息
- 关联 `report_runs` 查看已生成报告

## 4. API 设计

所有 API 返回 JSON。

### 通用约定

- 分页：`?page=1&page_size=50`
- 日期区间：`?start=YYYY-MM-DD&end=YYYY-MM-DD`
- 排序：`?sort=trade_date&order=desc`
- 错误格式：`{ "error": "...", "code": 400 }`

### 4.1 `GET /api/stocks`

返回股票列表，支持全部筛选条件。

```json
{
  "page": 1,
  "page_size": 50,
  "total": 6,
  "items": [
    {
      "symbol": "603605.SH",
      "market": "CN",
      "name": "珀莱雅",
      "latest_trade_date": "2026-08-07",
      "latest_close": 57.72,
      "price_adj_factor": 1.058587,
      "pe_ttm": 23.45,
      "pe_status": "ok",
      "pct_chg": -0.31,
      "active_card_id": "603605SH_120ca661",
      "tier_state": "tier1_within_3pct",
      "signal_count_5d": 2,
      "last_run_status": "success"
    }
  ]
}
```

### 4.2 `GET /api/stocks/{symbol}`

返回单股元数据 + 最新状态快照。

### 4.3 `GET /api/stocks/{symbol}/bars`

返回日线/周线数据。

```
?granularity=daily&start=2026-01-01&end=2026-08-07&price=unadjusted
```

`price` 选项：
- `unadjusted`：原始 `close_raw`
- `adjusted_back`：复权指标折回（`indicators_daily.*` 已是复权值，可直接用于叠加）
- `fully_adjusted`：复权收盘价 `close_raw * price_adj_factor`

### 4.4 `GET /api/stocks/{symbol}/indicators`

返回日线或周线指标序列。

```
?granularity=daily&start=...&end=...&fields=ma5,ma10,rsi12,pe_ttm
```

### 4.5 `GET /api/signals`

返回 `signal_facts`，支持完整筛选。

```
?symbols=603605.SH,002747.SZ&signals=daily_watch,tier_triggered&states=active,triggered&start=2026-06-01&end=2026-08-07
```

### 4.6 `GET /api/cards`

返回卡片版本。

```
?symbol=603605.SH&status=active&start=2026-01-01
```

### 4.7 `GET /api/compare`

返回多股同一指标的时间序列。

```
?symbols=603605.SH,002747.SZ&metric=pe_ttm&granularity=daily&start=...&end=...
```

### 4.8 `GET /api/runs`

返回 `pipeline_runs` / `report_runs`。

```
?stage=&status=success&start=...&end=...
```

## 5. 关键实现约束

### 5.1 复权口径在 UI 中的展示

系统内部指标全部基于复权价。UI 必须向用户明确当前展示口径：

| 模式 | 价格轴 | 均线/BOLL 来源 | 使用场景 |
|------|--------|----------------|----------|
| 不复权 | `close_raw` | 指标按 `price_adj_factor` 折回 | 与卡片价区、证伪线对比 |
| 完全复权 | `close_raw * price_adj_factor` | 直接使用 `indicators_daily` | 技术面比较、历史连续性 |

后端在返回 `/bars` 时，按需计算复权价格序列，不破坏数据库存储的口径。

### 5.2 日期字段约定

- `trade_date`, `week_end_date`, `observed_on` 等使用市场本地日期（YYYY-MM-DD）。
- UI 上所有时间戳统一按 `Asia/Shanghai` 或 `watchlist.timezone` 展示。
- 日期选择器默认范围：最近 90 个交易日，可在 `config/ui.yaml` 配置。

### 5.3 数据质量提示

- `pe_status` 不为空时，在 PE 图旁显示状态标签。
- `trading_status = suspended` 的股票标记为停牌。
- `signal_facts.state = incomplete` 时，列表行标黄/红。
- 任何 `pipeline_runs.status = degraded/failed` 在页面顶部 banner 提示。

### 5.4 性能约束

- 单股 3 年日线数据量不大，可直接全量返回。
- 多股对比/纯指标页限制最多 6 只股票、最多 6 个指标同时展示。
- 股票列表页默认只展示最新快照，不展开历史。
- API 使用参数化查询，禁止 SQL 拼接。

### 5.5 安全与权限

第一期不设登录：
- 所有页面只读。
- 后端不提供任何写入接口。
- 静态文件目录不得暴露 `.db` 或 `data/raw`。

## 6. 默认配置

新增 `config/ui.yaml`：

```yaml
app:
  host: 127.0.0.1
  port: 5000
  debug: false

defaults:
  page_size: 50
  recent_trading_days: 90
  default_indicators:
    - ma5
    - ma20
    - ma60
    - macd_hist
    - rsi12
    - pe_ttm

price_display:
  default: unadjusted  # 与卡片价区对比默认不复权
  options:
    - unadjusted
    - fully_adjusted

charts:
  library: echarts
  colors:
    - "#5470c6"
    - "#91cc75"
    - "#fac858"
    - "#ee6666"
    - "#73c0de"
    - "#3ba272"
```

## 7. 目录结构

```
scripts/ui/
├── app.py                 # Flask 应用入口
├── queries.py             # 所有 SQL 查询封装
├── __init__.py
├── templates/
│   ├── base.html          # 基础模板
│   ├── index.html
│   ├── stocks.html        # 股票列表
│   ├── stock.html         # 单股仪表板
│   ├── compare.html
│   ├── indicators.html
│   ├── signals.html
│   ├── cards.html
│   └── runs.html
├── static/
│   ├── css/
│   │   └── app.css
│   └── js/
│       ├── common.js      # 筛选条、日期处理
│       ├── stock.js       # 单股图表
│       ├── compare.js
│       └── indicators.js
config/ui.yaml             # UI 默认配置
```

## 8. 验收标准

- [ ] 股票列表页能按 symbol、名称、市场、PE 范围、涨跌幅、active 卡片状态等筛选。
- [ ] 单股页能切换日线/周线、切换复权口径、勾选任意指标叠加。
- [ ] 每个指标都按时间序列展示，鼠标悬停显示具体日期和数值。
- [ ] 信号时间轴能按类型/状态/日期筛选，并展开 `details_json`。
- [ ] 多股对比页支持最少 2 只、最多 6 只股票，同一指标对比。
- [ ] 运行状态页能展示 `pipeline_runs` 和 `report_runs`。
- [ ] 所有 API 只读，不暴露写接口。
- [ ] 页面能正确提示数据不完整/降级/停牌状态。

## 9. 给执行智能体的备注

- 先实现 `scripts/ui/queries.py` 和 `/stocks`、`/stock/{symbol}` 两个页面，作为最小可用版本。
- 再扩展 `/indicators`、`/signals`、`/compare`。
- 图表库优先用 ECharts，若实现者有偏好可替换为 Plotly.js，但需保持同一套配色和交互。
- 不要修改 `data/market.db` schema；若需要视图可在应用层用 Python 处理。
- 不要在 `app.py` 中写死任何股票 symbol；所有 symbol 均来自 `watchlist` 或用户输入。
- 价格口径（复权/不复权）在应用层计算，不要改动指标计算逻辑。
