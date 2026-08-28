# 股票分析系统设计

> 状态：实现基线 v2。本文定义第一版必须遵守的数据、时间、版本和运行契约；策略参数可以通过配置调整，但不得绕过这些契约。

## 1. 目标与边界

### 1.1 目标

- 对 watchlist 中的单只股票进行日线和周线分析。
- 根据估值排期卡，在每日盘后生成下一个交易日的观察点和决策点。
- 保存当时可见的数据、规则版本和判断依据，使历史报告与实际执行可审计。

策略内核为**估值排期卡框架**：估值锚定档（赔率管理）+ 胜率打分（仓位大小）+ 衰竭信号择时（时机确认）+ 证伪线兜底（认错机制）。具体研判流程由 `skills/fred-valuation-card-skill/` 承担。

### 1.2 非目标

- 第一版不做盘中实时交易，不接券商下单接口。
- 系统不预测涨跌，不输出买卖建议，只报告已定义条件是否满足。
- 财联社等全市场快讯、组合级风险预算和自动回测优化不进入第一版。

### 1.3 系统分层

1. 信息收集层：获取并保存不可变原始数据，确定性适配为统一数据模型。
2. 指标计算层：按明确口径计算可重现的日线、周线和估值指标。
3. 策略分析层：Python 生成确定性信号事实，LLM 基于事实和存档做受约束研判。
4. 输出层：生成单股报告和全池日报，不直接改变执行记录。

运行、版本、审计和数据质量约束贯穿四层，不单独形成业务层。

---

## 2. 贯穿契约

### 2.1 点时语义

任何历史日期 `as_of` 的计算只能使用 `available_at <= as_of` 的数据。禁止用数据库中“今天的最新值”覆盖历史当时可见的值。

统一使用以下时间字段：

| 字段 | 含义 |
|---|---|
| `event_at` | 事件实际发生时间，例如公告所述事项发生时间；未知时为空 |
| `published_at` | 来源正式发布内容的时间 |
| `available_at` | 系统允许该数据参与计算的最早时间 |
| `ingested_at` | 系统完成抓取的时间 |
| `as_of` | 某次计算或报告的数据截止时间 |

- 时间戳统一存 UTC，并同时保存来源时区；交易日期按市场本地时区确定。
- 日线行情在交易所收盘且数据通过完整性校验后才可用。
- 财务数据的 `available_at` 取正式披露时间，不取报告期截止日。
- 无可靠发布时间的数据不得自行假定为盘前发布，默认从下一个完整交易日开始参与事件研究。

### 2.2 数据生命周期

系统将数据分成四类：

1. **原始数据**：不可变，只追加。保存来源响应、请求参数、抓取时间和校验和。
2. **规范化事实**：允许因来源修订而 upsert，但每次变化必须能追溯到原始数据和修订记录。
3. **派生数据**：指标和信号事实，可按当前口径删除后重算。
4. **决策与执行数据**：卡片生效版本、报告输入快照和执行记录，不可覆盖，只能追加新版本或冲正记录。

### 2.3 版本与来源

每次计算至少记录：

```text
run_id
as_of
data_cutoff
adapter_version
config_hash
rule_version
card_version_id（涉及卡片时）
app_version 或 git_commit
```

派生表表达“按当前规则重算后的历史事实”；历史决策的原始依据由报告快照、卡片版本和 `executions.signal_snapshot_json` 冻结保存。

### 2.4 唯一事实源

- SQLite 是结构化状态的唯一事实源。
- `cards/` 和 `reports/` 下的 Markdown 都由数据库记录渲染，是不可手工回写数据库的存档或视图。
- LLM 不能直接覆盖当前生效卡片，只能生成待确认的卡片草稿。

### 2.5 失败原则

- 关键行情、交易日历、指标或当前卡片不完整时，该股票不得输出“条件已满足”，只能输出 `incomplete` 及原因。
- 新闻评价失败不阻断确定性价格信号，但报告必须标记消息面结果过期或缺失。
- 全池日报可以部分生成，但必须列出失败和数据过期的股票，不能静默沿用旧数据。

---

## 3. 第 1 层：信息收集层

### 3.1 职责边界

```text
Source Connector / Skill（外部交互）
  -> 调用 MCP、API 或网页
  -> 将来源原始响应和请求元数据写入 data/raw/

Python Source Adapter（确定性）
  -> 解析各来源格式
  -> 校验字段、时间、单位和主键
  -> 映射为统一数据模型
  -> 在事务中写入 SQLite

Python Derivation（确定性）
  -> 交易日完整性检查、复权因子、周线、指标和信号
```

- Skill 不计算指标、不评价消息、不生成规范化财务数字。
- 不要求 Python 完全不懂数据源。每个数据源有独立 adapter；更换数据源时只新增或替换 adapter，不修改下游统一模型与策略代码。
- 多源共用的确定性逻辑下沉公共层：标准公告线格式（title/time/url/source/summary/code/setcode/name）的解析引擎在 `scripts/adapters/announcements.py`，tdx/akshare 等源适配器只做薄壳委托并在 `events.source` + event_id 命名空间上隔离来源（§3.6 dedup）；代码映射/交易日推进类工具在 `adapters/common.py`。源间不互相借用实现，复用一律经公共层。
- 原始文件按 `data/raw/{source}/{data_type}/{YYYY-MM-DD}/{run_id}/` 存放，并在 `raw_objects` 登记路径、请求、响应校验和和抓取状态。

### 3.2 行情数据

**数据源**（2026-08-21 起通达信为第一优先源，kimi/yahoo 兜底）：

- **第一优先：`tdx-connector` MCP**（`tdx_kline`，A 股 setcode=1/0/2、港股 setcode=31、指数 setcode=62，含 amount 弥补 kimi 缺陷；`wenda_notice_query` 公告；`tdx_quotes hasCwInfo=1` 估值/股本/股东人数快照）。adapter `scripts/adapters/tdx.py`，采集规范 `skills/tdx-collect/SKILL.md`。
- A 股 fallback：`kimi-datasource` 的 `stock_finance_data.get_price`（ticker 形如 `600223.SH`，access_token 易失效需 `/login`，公告接口自 2026-08-13 持续 EMPTY_DATA）。
- 港股 fallback：插件内 `yahoo_finance`（ticker 形如 `0700.HK`）。首次接入港股必须用一只有除权历史的股票验证字段、时区和复权口径。
- tdx volume 单位为手（Unit=100），adapter 按 unit 列换算为股（与 kimi volume_raw 口径一致）；tdx amount 单位为元，直接入库（kimi 缺 amount，tdx 是优势字段）。

**范围**：

- 第一版采日线，初始化至少 3 年；周线由日线生成。
- **第一版历史 PE 刻度即 3 年样本**：3 年行情 + 3 年报/8 季报不支持 5–10 年跨周期刻度，卡片必须强制标注刻度样本区间（"刻度基于 YYYY-MM 至 YYYY-MM 的 N 个恐慌低点"），引用旧体系估值历史时同样标注。行情/财报向 5–10 年扩展列为二期（插件单次区间上限 3 年，需分段采集）。

**规范化字段**：

```text
symbol, market, trade_date,
open_raw, high_raw, low_raw, close_raw,
volume_raw, amount_raw, currency,
price_adj_factor, share_factor,
trading_status, source, raw_object_id, updated_at
```

校验规则：

- `low <= open/close <= high`，价格、成交量和成交额不得为负。
- 交易日必须存在于 `trading_calendar`；无 bar 时必须区分停牌、来源缺数和非交易日。
- 同一来源返回的重复记录必须内容一致，否则作为数据冲突处理。
- 来源修订历史行情时允许更新规范化事实，但必须写入 `data_revisions`，原始文件不变。

### 3.3 复权口径

系统内部使用明确的前向累积因子：

```text
adjusted_price_t = raw_price_t * price_adj_factor_t
adjusted_volume_t = raw_volume_t / share_factor_t
```

- `price_adj_factor` 在一个固定的 `factor_origin_date` 归一为 `1.0`，在除权除息生效日及之后累积变化，使该版本中的历史早期值保持稳定。这是本文所称的后复权序列。
- **术语对照**：本文"后复权"与通达信/东方财富等市面软件的"后复权"方向一致（锚定历史起点，历史值不随新除权变化）；市面"前复权"（锚定最新价、历史值随每次除权重算，对应数据源 `adjust=forward` 参数）在本系统只用于展示换算，不进入存储与信号计算。
- 后续增量不能因为三年窗口向前滚动而重新归一化。若需要向 `factor_origin_date` 之前扩展历史，必须创建新的 `adjustment_factor_version`，同时重建受影响的派生数据并在运行记录中说明。
- `share_factor` 只反映拆股、送转等股份数量变化，不包含现金分红，用于防止机械性股数变化制造虚假放量信号。
- 若来源只提供前复权价，adapter 应先计算“前复权价 ÷ 不复权价”的来源因子，再相对固定的 `factor_origin_date` 归一化为上述前向累积因子。
- 因子方向、归一化日、来源和生成算法写入 `adjustment_factor_versions`。因子无法验证时，相关股票不得生成依赖复权序列的信号。

增量抓取至少包含最近 5 个交易日的重叠窗口，用于发现来源修订或新除权。发现因子变化时，重建该股票全部保留区间的因子、周线、指标和信号；不得只修改最后几日。

### 3.4 周线

- `weekly_bars` 只保存完成周。完成周由 `trading_calendar` 判断，不写死星期五。
- 技术周线从逐日复权后的 OHLC 和调整后成交量聚合：开盘取周首日、收盘取周末日、高低取极值、成交量和成交额求和。
- 不得先聚合不复权 OHLC 再乘周末单一因子，因为除权发生在周中时会扭曲周高、周低和 K 线形态。
- 若周线信号识别出价格锚点，同时记录锚点交易日的 `adjusted_price` 和 `raw_price`。技术比较使用复权价，排期卡价区比较使用该日不复权价。

### 3.5 交易日历与指数

- 独立维护 `trading_calendar`，字段包括 market、trade_date、开闭市时间、是否完整交易日和特殊状态。
- **日历来源与降级**：交易日历由交易所年度休市安排生成（A 股：上交所/深交所每年 12 月发布次年休市安排；港股：港交所年度交易日历，含半日市）。落地形态为年度种子文件（`config/calendar_{market}_{year}.yaml`），每年初一次性导入，不依赖易碎接口。每日用指数行情交叉校验（指数有 bar 而日历说休市，或反之 → 标记冲突，相关股票输出 `incomplete` 及原因，不猜）。
- 指数行情不能作为唯一交易日历来源，只用于完整性交叉检查和超额收益对照。
- A 股默认基准为沪深 300 `000300.SH`，港股默认基准为恒生指数 `^HSI`；`watchlist.benchmark_code` 可按股票覆盖。
- 指数日线至少保存 O/H/L/C、成交量、币种、来源和可用时间，每日增量抓取。

### 3.6 公告与新闻

**来源矩阵**（●=一期已备 adapter，◐=一期已设计未实现，○=二期新增）：

| 来源 | 渠道 | 覆盖层面 | 状态 |
|---|---|---|---|
| A 股公告 | tdx wenda + 巨潮 cninfo（`stock_zh_a_disclosure_report_cninfo`，akshare 采集器 `--sources` 默认含 announcement） | 公司 | ●（已接 daily，2026-08-28；事件带 source_tier=1，r2 §2.1） |
| 港股公告 | 港交所披露易 | 公司 | ◐ |
| A 股个股新闻 | 历史：新浪个股新闻页；增量：东财搜索 API | 公司 | ◐ |
| 港股新闻 | 东财搜索，接受历史深度有限并在报告中标注覆盖区间 | 公司 | ◐ |
| 全市场快讯 | 财联社（政策/宏观主渠道，`--sources telegraph`） | 政策、宏观 | ●（采集/入库通道已备，事件带 source_tier=4；持续采集编排属 r2 Phase 2） |
| 行业新闻 | 东财行业频道 | 行业 | ○ |
| 财报披露预约 / 解禁日程 | akshare `stock_report_disclosure` / `stock_restricted_release_queue_em` → `event_calendar`（`--sources calendar` 手触发） | 日历层 | ●（r2 Phase 1，2026-08-28） |
| 宏观因子（商品/外汇） | akshare sina 期货接口 + 中行牌价（清单 config/macro_factors.yaml） | 宏观 | ●（r2 Phase 2，2026-08-28：macro_factors 每日快照，进默认 sources） |
| 龙虎榜 / 大宗交易 | akshare（data.eastmoney.com/datacenter-web，不踩 push2） | 资金/情绪（flow） | ●（r2 Phase 2，2026-08-28：events scope='flow' tier=3 静默入库，不推送不进日报） |
| 全市场行业归属 | `scripts/collect/industry_collect.py`（push2delay 域，季度刷新手触发）→ symbol_industry | 关联层 | ●（r2 Phase 3，2026-08-28，5641 只；与 watchlist.industry_code 同口径） |

- 每个来源接入时必须在报告中标注覆盖区间；快讯/行业源缺数时消息面标 `degraded`，不阻断确定性价格信号（§5.5 既有原则）。

规范化事件只保存事实字段，不混入 LLM 评价：

```text
event_id, event_type, published_at, available_at,
title, summary, canonical_url, source, source_external_id,
content_hash, raw_object_id
```

- 股票关联放在 `event_symbols(event_id, symbol)`，支持一条事件关联多只股票。
- **行业/政策/宏观事件的个股关联（二期）**：`watchlist` 扩展 `keywords`（人工维护，如「黄金→紫金/豫光」「航油→南航」）做初筛，LLM 可建议关联但必须人工确认后才写 `event_symbols`——与卡片 draft-only 同纪律，不允许 LLM 自动关联直接生效。
- 优先用来源 ID 去重，其次使用规范化 URL 和内容哈希；URL 查询参数不能直接作为唯一身份。
- PDF、HTML 和新闻正文均作为不可信内容处理，进入 LLM 前移除脚本和隐藏文本，不允许正文中的指令改变系统提示或工具调用。

**二期上线子序**（§9.4 第 4 步的展开）：① 公告采集入库（adapter 现成）→ ② 个股新闻 adapter → ③ LLM 评价 + 资讯流 → ④ 快讯/行业源 + scope 关联。

### 3.7 财务、股本、预测与汇率

财务报告必须保留修订版本：

```text
report_id, symbol, period_end, period_type, fiscal_year,
published_at, available_at, revision, currency, unit,
is_cumulative, raw_object_id
```

财务事实通过 `report_id` 关联，至少包括营收、归母净利、基本/稀释 EPS、期末已发行股数和期末流通股数。相同报告期的更正报告新增 revision，不覆盖旧版本。PE 使用哪一种股数必须在配置中明确，默认使用已发行股数，不得在运行中临时切换。

- 初始化读取最近 3 个年报和最近 8 个季报或中期报告；按股票实际财年处理，不能假定所有港股以 12 月为财年末。
- `share_capital_events` 保存增发、回购注销、送转和转股导致的已发行股数变化，包含 `effective_at` 与 `available_at`；若数据源只提供流通股数，必须标注 `share_count_type`，不得当作总股本使用。`share_count_type` 取值：`issued`（已发行股数，yahoo 快照口径，A/H 双上市公司实际只含 A 股）、`float`（流通股）、`group_total`（A+H 集团总股本，stock_finance_data `ths_total_shares_stock` 口径，vendor 通用 PE 股本口径）。PE 取数规则：`effective_at <= as_of` 最新事件，同一 `effective_at` 多口径并存时优先 `group_total`，无则回退 `issued`（2026-08-17 起为 watchlist 全部 13 只写入 group_total 单点快照，修复 A/H 公司 PE 分母只用 A 股股本的口径缺陷；单点快照假设在 details_json 标注）。
- `forecasts` 保存每次抓取快照，不只保留最新一批；历史查询必须选择 `snapshot_at <= as_of` 的最新快照。
- `fx_rates` 保存财务币种到交易币种的日汇率。币种不一致且缺少汇率时，不计算 PE。

**三张支撑表的来源与降级**：

- `fx_rates`：yahoo_finance `get_historical_stock_prices` 支持外汇（财务币种→交易币种方向，如人民币计财报的港股用 `CNYHKD=X`；来源只有反向对时取倒数入库，换算方向在 adapter 内统一，不在下游临时判断），历史上限 2 年——PE 只用近期汇率，2 年窗口第一版够用；每日随行情增量抓取。降级：缺当日汇率时用最近一个可用交易日汇率并在报告标注；超过 5 个交易日无更新则 PE 返回空值。
- `share_capital_events`：三个来源按优先级——① yahoo_finance `get_stock_actions`（分红与拆股事件流，覆盖 A/H）；② 相邻两期 `financial_facts` 期末已发行股数变化反推（增发/回购注销只能靠这个，get_stock_actions 不含）；③ 3.3 重叠窗口检测到的因子突变交叉印证。任一来源缺失时其余来源仍可生成事件，但 `details` 标注来源组合；股本无法确认时 PE 与市值口径返回空值（遵循 2.5）。
- `trading_calendar`：见 3.5（交易所年度休市安排种子文件 + 指数交叉校验）。

TTM 在任意 `as_of` 上按当时可见的最新修订计算：

```text
若最新报告为年报：
  TTM = 最新年报归母净利

若最新报告为本财年累计中报/季报：
  TTM = 上一财年年报 + 本财年最新累计值 - 上一财年同期累计值
```

三个组成项都必须在 `as_of` 时可见；任一缺失时 TTM 为空，不用今天已知的数据补历史空洞。

---

## 4. 第 2 层：指标计算层

### 4.1 指标及口径

| 类别 | 指标 | 默认参数 | 口径 |
|---|---|---|---|
| 均线 | MA | 5/10/20/60/120/250 | 复权收盘价简单移动平均 |
| MACD | DIF/DEA/柱 | 12/26/9 | EMA `adjust=False`，柱=`2*(DIF-DEA)` |
| RSI | RSI | 6/12/24 | Wilder RMA |
| 布林带 | BOLL/带宽 | 20, 2 | 总体标准差 `ddof=0` |
| 成交量 | 均量/均值/标准差 | 5/10/20/60 | 调整后成交量 |
| KDJ | K/D/J | 9/3/3 | 初始 K/D=50，零振幅沿用前值 |
| 估值 | PE(TTM) | 点时口径 | 不复权市值 ÷ TTM 归母净利 |
| 基础量 | 涨跌幅/振幅 | 无 | 复权 OHLC |

- 所有滚动窗口默认要求完整窗口，样本不足返回空值，不用较短窗口冒充。
- 成交额若可用，另计算同参数的均值和标准差；成交额不做股份因子调整。
- 信号判定中的“历史均值”统一对序列 `shift(1)` 后计算，排除正在判断的当前 bar。
- `pe_ttm = close_raw × 当日已生效股本 ÷ 已换算到交易币种的 TTM 归母净利`。股本口径取 `group_total`（A+H 集团总股本）优先、回退 `issued`（§3.7）。TTM 小于等于 0、股本或汇率缺失时，PE 为空并保存原因码。

### 4.2 参数和实现版本

- 指标参数保存在 `config/indicators.yaml`，信号参数保存在 `config/signals.yaml`。
- 配置支持 `defaults` 和 `overrides.{symbol}`。第一版全部使用默认值；个股覆盖必须记录理由、日期、批准人和回测外验证依据。
- 每次运行计算配置文件的内容哈希。改变公式实现时增加 `rule_version`，只改阈值时改变 `config_hash`。
- pandas 及关键依赖版本写入运行记录，公式边界由固定样本的 golden tests 锁定。

### 4.3 计算和存储

- 每日行情入库后，对受影响股票全量重算保留区间；约 3 年日线的数据量适合这种做法。
- `indicators_daily` 主键为 `(symbol, trade_date)`，`indicators_weekly` 主键为 `(symbol, week_end_date)`，并保存 `run_id/rule_version/config_hash`。
- 指标表保存当前口径的可重算结果。实际执行和已发布报告引用的指标值另存输入快照，不依赖日后重算结果。
- 周线指标只使用完成周。

---

## 5. 第 3 层：策略分析层

### 5.1 信号事实原则

Python 负责输出确定性、可测试的 `signal_facts`：

```text
symbol, observed_on, signal, state,
anchor_id, triggered, active_until,
details_json, run_id, rule_version, config_hash
```

- 历史价格类信号逐日或逐周按当时可见数据计算，禁止用后来形成的低点反推此前信号。
- 卡片相关信号只对卡片实际生效区间计算，禁止把当前卡片应用到历史日期。
- `details_json` 使用按 signal 类型定义的 JSON Schema，至少包含参与判断的日期、原值、阈值、锚点和原因码。

### 5.2 周线锚点

在每个 `as_of` 上独立识别：

- **恐慌低点**：最近一次有效恐慌型信号对应周内最低复权价的交易日。若没有恐慌型信号，使用过去 26 个完成周中最低复权收盘价所在周，并在周内定位最低复权价交易日作为 fallback 锚点。
- **下跌起点**：从恐慌低点向前的 26 个完成周内，最高复权收盘价所在周；平值时取离恐慌低点最近的一周。
- 每个锚点保存识别时的复权价、不复权价、日期和 `as_of`。fallback 发生变化时生成新 `anchor_id`，不覆盖旧执行快照中的锚点。

⚠️ **待观察调整**：26 周回溯窗、下跌起点后前 4 周均量、50% 缩量阈值均为第一版默认值，非唯一正确答案。fallback 锚点永远存在——震荡/上涨市中的小幅回调也会凑出"干涸型/持续时间"等伪 episode，episode 结束规则（收盘高于下跌起点收盘）只能事后终止。日报必须带出锚点明细（锚点日期、起点日期、计算值），人工核对数周后再考虑调参；所有参数入 `config/signals.yaml`。

### 5.3 衰竭信号

以下是第一版默认公式，所有数值进入 `config/signals.yaml`：

1. **恐慌型**：当前完成周调整后成交量不低于此前 20 个完成周均量的 2 倍，并满足以下形态之一：
   - 长下影：下影长度不低于实体 2 倍，且不低于全周振幅的 35%；
   - 大阳线：收盘高于开盘，实体不低于全周振幅的 60%，周涨幅不低于 5%。
2. **干涸型**：当前完成周调整后成交量不高于下跌起点后前 4 个完成周均量的 50%。不足 4 周时不判定。50% 是第一版的确定值，允许在 40% 至 60% 范围内通过版本化配置修改。
3. **三周不创新低**：恐慌低点之后已有 3 个完成周，且这 3 周复权最低价均未低于锚点复权最低价。
4. **周线底背离**：使用左右各 2 个完成周确认 pivot low。最近两个已确认 pivot low 位于 26 周窗口内，后一个复权收盘价低于前一个，同时 RSI(12) 或 MACD 柱高于前一个。信号记录在 pivot 被确认的周，不回填到 pivot 发生周。
5. **持续时间**：从下跌起点到当前已达到 8 个完成周。

活跃期限：

- 恐慌型和底背离从确认周起保持活跃 4 个完成周。
- 干涸型只在条件持续满足时活跃。
- 三周不创新低在再次创新低前持续活跃。
- 持续时间信号在当前下跌 episode 内持续活跃。
- 当周线收盘高于下跌起点收盘，或人工确认卡片进入新的价格 episode 时，旧 episode 结束，旧信号不再计入当前得分。

“衰竭信号至少 2 项”指同一个 `anchor_id` 下，在当前完成周仍处于 active 状态的不同信号数量。

### 5.4 日频监测与状态机

所有排期卡价区、证伪线、箱体和现价使用不复权口径。需要与均线比较时，将复权均线除以当日 `price_adj_factor` 后再比较。

默认规则：

- **证伪线有效跌破**：连续 2 个交易日收盘价低于证伪线 1% 以上。阈值和确认日数可配置。
- **档位临近**：现价距离目标价区最近边界不超过 3%。
- **档位触发**：收盘价进入价区；第二、三档还要求同一锚点的活跃衰竭信号不少于 2 项。
- **波段箱体**：只监测存档边界，不每日自动重新识别。边界变化必须生成新卡片版本。

右侧确认采用显式状态机，关键位由卡片保存，不由日报临时猜测：

```text
idle
  -> waiting_retest：收盘突破关键位 1%，且成交量 >= 前 20 日均量 2 倍
  -> confirmed：10 个交易日内回踩关键位上下 2%，且收盘不低于关键位 1%
  -> invalidated：等待期间收盘低于关键位 1%
  -> expired：10 个交易日内未发生合格回踩
```

每次状态转换写入 `signal_facts`，保存起始日期、截止日期、关键位、容差和成交量明细。

### 5.4b 公司行为处置（除权 × 生效卡片）

卡片价区、证伪线、箱体、右侧触发位均为不复权绝对价位。生效卡片存续期间发生除权（送转、拆并股、现金分红——凡使 `price_adj_factor` 变化的事件）时，不复权现价发生非基本面跳变，会产生伪触发（如 10 送 10 后现价腰斩被误判为"证伪线有效跌破"）。

**小额现金分红快速通道**：现金分红导致的除权价变动比例低于阈值（默认 2%，入 `config/signals.yaml`）时，不冻结、不等待确认——Python 直接按每股分红额机械换算生成新卡片版本并自动激活（`supersedes_id` 与换算明细照常记录），当日日报标注"分红自动换算"即可。分红是每股每年至少一次的常规事件，小额分红对价区的影响低于证伪线阈值本身，逐次人工确认收益极低。送转、拆并股及达到阈值的大额分红走以下完整三段式：

**第一步：即时冻结（检测当日生效）**

- 3.3 的重叠窗口发现该股因子变化/新公司行为后，自事件生效日起，日频监测对该股**暂停一切卡片相关触发输出**（档位、证伪线、箱体、右侧状态机挂起），报告输出 `suspended_corporate_action` 及事件明细，不输出伪触发。
- 技术信号（衰竭信号、均线、锚点）工作在复权序列上，天然连续，**不受冻结影响**。

**第二步：机械换算生成新 draft（Python，不经 LLM）**

送转/拆股是纯单位变化，不触碰估值逻辑（PE 刻度、胜率、档位比例原样有效），因此换算为确定性工作：

- 三档价区、证伪线、箱体边界、右侧触发位/止损位 × 股份倍率的倒数（10 送 10 → ×0.5）；
- EPS 三情景 ÷ 倍率（每股口径同步变化）；
- PE 情景、胜率区间、档位比例、持有期假设不变；
- 现金分红：价格类字段按每股分红额做减法换算（除权价 = 原价 − D）；
- 产物为新卡片 draft：`supersedes_id` 指向旧卡，`input_snapshot_json` 记录换算来源版本、事件与倍率/金额。

**第三步：人工确认激活**

- 换算 draft 在当日日报列为最高优先级决策点（见 6.3）。确认后激活新版本、关闭旧版 `effective_to`，监测恢复；未确认前持续冻结。
- draft 被拒绝时才升级到排期卡 skill 全量复核。
- `executions` 历史记录保留原始成交价与当时 `card_version_id`，不回溯换算；审计沿 `supersedes` 链与事件倍率重现。

### 5.4c 吸筹形态状态机（观察点）

方法来源《如何看出主力吸筹》三阶段框架（放量破位 → 缩量横盘 → 试盘 → 放量突破确认），做成日线级确定性状态机（`scripts/signals/accumulation.py`），写 `signal_facts`（`signal="accumulation"`），每日一行，状态取值 `idle / watching / consolidating / confirmed / failed`（terminal 次日回 idle，同右侧状态机纪律）。

- **放量破位**（idle 内检查）：单日跌幅 ≥ 5%，且调整量 ≥ 前 20 日均量 ×2.0（shift(1) 纪律），且收盘创 60 日新低 → `watching`。
- **缩量横盘**（watching，破位满 10 个交易日起判定）：破位日至当日窗口振幅 ≤15%，窗口均量 ≤ 破位基数均量 ×0.8，MA5/10/20 粘合（(max−min)/MA20 ≤ 5%，MA 缺失当日不判定）→ `consolidating`；箱体取窗口收盘价上下界（避免影线毛刺）。破位后超 120 个交易日未确认 → `failed(expired_no_consolidation)`。
- **试盘计数**（consolidating 内）：振幅 ≥3%、上影 ≥ 全日振幅 50%、量 ≥ 前 20 日均量 ×1.5 → `probe_count+1`（只计数，不产生状态转换）。
- **确认/失效**：放量（×1.5）阳线收盘突破箱体上沿 → `confirmed`；收盘跌破箱体下沿 → `failed(box_broken)`；横盘超 120 个交易日 → `failed(expired_consolidation)`。

边界与纪律：

- 价用复权、量用 `volume_raw / share_factor`（与 §4.1 指标口径一致）；逐日只用当日及之前数据，无未来函数。
- **仅为日报观察点**：不进排期卡触发逻辑，不改衰竭信号"≥2 项释放档位"口径。形态识别误报天然偏高（下跌中继与吸筹前期同形），输出只供人工参考。
- 分时与五档盘口数据当前数据源不可得，"试盘"仅为日 K 级代理，精度低于原方法；盘口"夹板/托单"判据不实现。
- ⚠️ 全部参数（`config/signals.yaml` 的 `accumulation` 节）为第一版默认值，需人工核对数周后才可调整（同 5.2 纪律）。

### 5.5 消息评价与事件研究

LLM 对新增事件输出符合 JSON Schema 的评价，写入独立的 `event_assessments`：

```text
event_id, assessment_version, model, prompt_version,
assessed_at, event_type, scope, direction, materiality,
confidence, rationale, status
```

- `scope` 为主题维度（二期新增）：`company / industry / policy / macro`，与 `direction`（正面/负面/无影响）正交；资讯流每条资讯展示 `direction × scope` 双标签。
- 原始事件表不保存或覆盖评价列。
- 同一事件可以因模型或提示词升级产生多个评价版本；报告明确使用哪个版本。
- 低置信度、正文缺失或多个重大事件重叠时，状态标记为 `needs_review`，不强行归因。

事件研究采用保守时点：

- 基准收盘价取 `available_at` 之前最后一个完整交易日收盘价。
- T+1/T+5 取 `available_at` 之后第 1/5 个完整交易日的复权收盘价，并使用同日期的 benchmark 收盘价计算超额收益。
- 股票停牌导致终点无价格时结果为空并标记 `suspended`，不自动顺延冒充 T+1/T+5。
- 事件研究是描述性证据，不宣称单一新闻与收益之间存在因果关系。

确定性事件研究已实现（`scripts/signals/event_study.py`，2026-08-14）：不写 LLM 评价列，只写 event_study_json（assessment_version='event_study_v1'、model='deterministic'）。交易日历用 trading_calendar（CN）权威开市日，index_bars（000300.SH）仅作基准价格——指数在开市日缺 bar 落 degraded（bench_missing），不静默顺延；个股开市日无 bar 为停牌 suspended；终点日 > 数据截止（个股与基准最大 bar 日取小）标 pending。幂等：degraded/pending 行重跑重算（数据回补后自动转正），完整 ok/suspended 行跳过；LLM 评价使用其他 assessment_version 命名空间，互不影响。

### 5.6 排期卡版本

`strategy_card_versions` 保存不可变版本，主要字段包括：

```text
card_version_id, symbol, status, schema_version,
created_at, effective_from, effective_to, supersedes_id,
currency, price_basis,
earnings_scenarios_json, valuation_scenarios_json,
price_tiers_json, invalidation_json, swing_box_json,
right_side_trigger_json, next_review_at,
input_snapshot_json, run_id
```

- 状态为 `draft/active/superseded/rejected`。同一股票同一时刻最多一个 active 版本。
- LLM 或排期卡 Skill 只创建 draft；经明确确认后才能激活。激活新版本时关闭旧版本的 `effective_to`。
- Markdown 路径为 `cards/{symbol}/{effective_from}_{card_version_id}.md`；`cards/{symbol}/current.md` 是自动生成的当前视图。
- 盈利底稿、估值刻度和胜率更新通过新版本增量修订，不修改旧版本。

### 5.7 执行记录

`executions` append-only，至少包括：

```text
execution_id, idempotency_key, symbol, executed_at,
action_type, tier, price, quantity, fees,
card_version_id, signal_snapshot_json,
reverses_execution_id, created_at
```

- 各档是否执行从有效执行记录推导，不由 `signal_facts` 推导。
- 错录通过新增冲正记录修复，不更新或删除原记录。
- 系统只记录人工确认的实际动作，不因信号触发自动生成成交记录。

---

## 6. 第 4 层：输出层

### 6.1 产物

| 产物 | 频率 | 用途 |
|---|---|---|
| `reports/{symbol}/{trade_date}.md` | 每日盘后 | 单股完整存档 |
| `reports/daily/{trade_date}.md` | 每日盘后 | watchlist 全池触发、临近触发和数据异常 |
| 周报附加段 | 每个市场当周最后交易日 | 衰竭打分与胜率复核 |

每份报告在 `report_runs` 保存 `run_id/as_of/card_version_id/rule_version/config_hash/input_snapshot_json/status`。已发布报告不因后续重算而覆盖；需要修订时生成 revision。

### 6.2 单股报告结构

1. **运行状态**：数据截止时间、完整或降级状态、当前卡片和规则版本。
2. **当前定位**：现价、所处档位、距下一边界百分比、箱体位置。
3. **决策点**：已触发的证伪、档位、右侧确认、箱体边界和锚复核。没有触发时明确写“今日无决策点”。
4. **观察点**：下一交易日的临近度、尚缺哪个条件、财报或公告窗口及新消息摘要。
5. **日历与消息面**（r2 Phase 1，2026-08-28）：`### 日历提醒（默认 3 日内）`——
   `event_calendar` 到期项（披露预约/解禁/宏观种子，窗口按每行 `remind_before_days`
   含两端边界）union 该股 active 卡 `next_review` 到期；`### 公司公告`——当日
   `available_at` 日期 == 报告日的公司公告，置顶标"需读原文"，无公告写
   "今日无新增公告"。"消息面"（LLM 初判+人审）子节属 r2 Phase 3，暂不占位。
6. **衰竭信号**：当前 `anchor_id`、各信号状态、失效时间和计数。
7. **指标快照**：关键日线、周线、量能和估值值。
8. **来源与异常**：数字来源、截止日期、缺失字段和过期评价。

### 6.3 全池排序

全池日报采用确定性优先级，不由 LLM 自由排序：

1. 数据不完整或运行失败。
2. 证伪线有效跌破、卡片复核逾期、公司行为换算 draft 待确认。
3. 已确认的档位、右侧或箱体决策点。
4. 距触发边界 3% 以内的观察点。
5. 新重大消息和普通状态更新。

同级按距触发边界百分比、事件重要性和 symbol 排序，并在报告中显示排序原因。

### 6.4 语言纪律

- 使用“触发、证伪、释放仓位、冻结、待确认”等规则语言，不使用“看涨、看跌、建议买入”等预测或建议语言。
- 每个关键数字标注来源和截止日期。
- LLM 生成的摘要不能新增事实、阈值或卡片条件；所有数字必须来自结构化输入。

---

## 7. 存储设计

数据库文件：`data/market.db`。SQLite 使用外键、WAL 模式和事务；schema 变更通过版本化 migration 管理。

| 分组 | 表 | 生命周期与用途 |
|---|---|---|
| 运行 | `pipeline_runs` | 每次流水线的阶段、截止时间、版本和错误 |
| 原始 | `raw_objects` | 不可变来源文件索引、请求元数据和校验和 |
| 原始 | `data_revisions` | 规范化事实发生变化的前后值和来源 |
| 配置 | `watchlist` | 股票、市场、时区、币种和 benchmark |
| 日历 | `trading_calendar` | 各市场交易日和特殊交易状态 |
| 行情 | `daily_bars` | 当前规范化不复权日线及复权因子 |
| 行情 | `corporate_actions` | 除权除息、拆并股、送转等公司行为事实 |
| 行情 | `adjustment_factor_versions` | 因子算法、归一化和版本 |
| 行情 | `weekly_bars` | 已完成周的复权技术周线 |
| 行情 | `index_bars` | benchmark 日线 |
| 事件 | `events` / `event_symbols` | 公告、新闻及股票关联 |
| 事件 | `event_assessments` | 版本化 LLM 评价和事件研究 |
| 财务 | `financial_reports` / `financial_facts` | 点时可追溯的财报及修订 |
| 财务 | `share_capital_events` / `fx_rates` | 每日市值口径支撑 |
| 财务 | `forecasts` | 分析师预测历史快照 |
| 派生 | `indicators_daily` / `indicators_weekly` | 当前口径指标，可重算 |
| 派生 | `signal_facts` | 当前口径信号事实，可重算 |
| 策略 | `strategy_card_versions` | 不可变卡片版本及当前状态 |
| 策略 | `executions` | 不可变实际执行与冲正 |
| 输出 | `report_runs` | 报告状态、输入快照和 revision |

所有 JSON 列必须有对应 JSON Schema，并在写库前校验。金额、价格、股数和汇率使用定点数或明确精度的 Decimal，不用二进制浮点直接持久化关键决策值。

目录结构：

```text
skills/                       # 外部数据调用和排期卡 Skill
scripts/adapters/             # 各来源确定性适配器
scripts/pipeline/             # 入库、校验、调度和报告流水线
scripts/indicators/           # 指标实现
scripts/signals/              # 信号与状态机
config/                       # 参数及 JSON Schema
data/market.db                # SQLite
data/raw/                     # 不可变原始响应
cards/{symbol}/               # 卡片版本 Markdown
reports/{symbol}/             # 单股报告
reports/daily/                # 全池日报
tests/                        # 单元、集成和 golden tests
```

---

## 8. 运行节奏与事务边界

### 8.1 每日盘后

1. 根据 `trading_calendar` 创建 `pipeline_run`，确定各市场本次 `as_of`。
2. 抓取行情和指数，保存 raw object，以重叠窗口适配入库。
3. 校验当前交易日是有行情、停牌还是缺数，完成关键数据质量门禁。
4. 在单一事务中发布规范化行情和因子版本。
5. 重算受影响股票的周线、指标、信号和状态机。
6. 抓取新增公告、新闻并运行版本化评价；失败时标记消息阶段 degraded。
   （当前实现：确定性部分——池级事件研究 event_study（§5.5，event_study_v1）
   已接入 daily，位于逐股信号之后、报告之前，池级事务，失败记 degraded 不阻断
   报告；**r2 Phase 3 LLM 评价链已接入 daily 步骤 6b**（6b1 事件级初判 → 6c
   关联层 → 6b2 逐股叙事，`scripts/llm/eval.py`，gate 按 §6.3），默认
   `config/llm.yaml enabled=false`——关闭态记 success+notes（设计关闭非
   degraded），配置 api key 后启用；公告/电报 source_tier 已随采集写入；flow
   （龙虎榜/大宗 tier=3）静默入库不推送不进日报；event_calendar 到期项在报告
   "日历提醒"与 /cards 横幅展示。）
7. 生成单股报告，再汇总全池日报。
8. 保存报告输入快照和各阶段状态，结束 pipeline run。

单只股票的行情、指标发布必须原子化。中途失败时保留上一次成功版本供查询，但新报告不得把旧版本伪装成本日结果。

信号阶段事务边界（2026-08-23 起）：基础阶段（入库→因子→周线→指标）单一事务，失败整体回滚；信号模块（weekly_signals → daily_watch → right_side → accumulation → corporate_action）各自独立子事务顺序执行，单模块异常只回滚该模块（不残留部分写入）、记 incomplete 并跳过后续模块（避免基于前序失败留下的旧派生数据继续判定），已成功模块的提交保留（信号为派生数据可重算，§2.2 第 3 类）。

### 8.2 周度和季度

| 触发 | 动作 |
|---|---|
| 市场当周最后交易日 | 更新衰竭信号、胜率重估和未执行档位的提前/冻结草稿 |
| 新财报 `available_at` 生效后 | 更新 TTM 与盈利底稿，生成锚复核 draft |
| `next_review_at` 到期 | 即使没有新财报也生成复核提醒，不自动延后 |

### 8.3 幂等与重试

- 每个外部请求和执行动作使用 `idempotency_key`。
- 相同 raw content hash 不重复解析；相同 pipeline 阶段可以安全重跑。
- LLM 调用超时或格式校验失败可有限重试，仍失败则记录 degraded，不写入半成品评价或卡片。
- 数据库写入先进入事务，全部校验通过后提交。

---

## 9. 第一版验收标准

### 9.1 点时与数据

- 财报更正后，能够分别重建更正前和更正后的历史可见值。
- 除权发生在周中时，日线和周线技术序列连续，且卡片仍使用不复权现价比较。
- 生效卡片存续期间发生送转/分红时：卡片触发即时冻结不输出伪触发，换算 draft 机械生成，确认激活后监测恢复，历史执行记录保持原口径可审计。
- 股票停牌、指数缺数和来源抓取失败不会被误判成非交易日。
- 港股财务币种与交易币种不一致时，PE 使用当时汇率或明确返回空值。

### 9.2 信号

- 每个阈值边界都有单元测试，包括等于阈值、略高和略低三种情况。
- 底背离只在 pivot 确认日出现，不回填形成未来函数。
- 右侧确认能覆盖 confirmed、invalidated 和 expired 三条路径。
- 配置改变后当前信号可重算，但旧报告与执行记录仍能显示原配置和原输入。

### 9.3 卡片、执行与报告

- 同一天可以创建多个卡片 draft，但任一时刻最多一个 active 版本。
- 激活新卡、拒绝草稿和冲正执行均不修改历史记录。
- 任一关键数据缺失时，报告显示 `incomplete/degraded`，不输出伪触发。
- 报告中的每个决策点都能追溯到卡片版本、信号明细、配置哈希、原始来源和数据截止时间。

### 9.4 上线顺序

1. 先完成行情、日历、复权、指标和数据质量门禁。
2. 再完成确定性信号、卡片版本和报告快照。
3. 用人工构造和真实历史样本验证数周。
4. 最后接入新闻 LLM 评价、胜率重估和卡片 draft 自动生成（二期子序与 scope 维度见 §3.6；`event_assessments` 加 `scope` 列需配套 migration，随二期第 ③ 步实施）。

在前三步通过验收前，不启用 LLM 自动更新策略状态。

**二期可选扩展**：采集资产负债表自算历史 PB 序列（一期 PB/PS 仅用 forecast 快照单点值，单股页标注来源与快照日期）。

### 9.5 第一版实现分级（硬门槛 / 软约束）

**硬门槛（第一版必须做到，无降级）**：

- 点时语义：历史计算只用 `available_at <= as_of` 的数据；财务按披露日对齐。
- 原始数据不可变、append-only；报告与执行记录不被重算覆盖。
- 卡片版本机制：LLM 只产 draft，人工确认激活，任一时刻最多一个 active。
- 失败原则：关键数据缺失输出 `incomplete/degraded`，禁止伪触发（含 5.4b 公司行为冻结）。
- 幂等：pipeline 阶段可安全重跑；相同 raw content hash 不重复解析。
- 核心公式 golden tests：复权、周线聚合、TTM、PE、衰竭信号阈值边界。
- 口径纪律：不复权现价 vs 复权指标值比较时按当日因子换算（5.4）。

**软约束（方向正确但第一版允许降级实现，违反时记 TODO）**：

| 项 | 第一版降级做法 | 完整形态（二期） |
|---|---|---|
| `data_revisions` 表 | 检测到来源修订 → 全量重建该股规范化事实，修订事件写入 `pipeline_runs` 日志 | 逐字段前后值追溯表 |
| `adjustment_factor_versions` 多版本 | 单一因子版本 + 固定归一化日；需前扩历史时整体重建并记日志 | 多版本共存与版本切换 |
| 报告 revision 机制 | 已发布报告文件不覆盖；修订 = 新文件 + 日志说明 | 结构化 revision 表与 diff |
| Decimal 持久化 | 关键决策值（卡片价区、执行价、汇率）以定点数/TEXT 存储；展示用中间值允许 REAL | 全字段定点精度 |
| JSON Schema 全覆盖 | 先覆盖卡片 draft 与消息评价两类 LLM 产物；其余 JSON 列做基本类型校验 | 全部 JSON 列 Schema 校验 |
