# 数据库设计与表字段说明

> 主库：SQLite `data/market.db`。Schema 由 `scripts/pipeline/migrations/` 管理（当前 0001_init.sql = schema v1），已应用版本记录在 `schema_migrations` 表。设计原则见 `docs/system_design.md` §2.2（生命周期）、§2.3（版本字段）、§7（存储设计）；本文档是逐表逐字段的速查。

## 1. 通用约定

- **生命周期六类**（§2.2，决定能否覆盖/重算）：
  - `[原始]` 不可变，append-only；
  - `[事实]` 规范化事实，来源修订允许 upsert 但须可追溯（data_revisions / raw_object_id）；
  - `[派生]` 可按当前口径 DELETE 后全量重算；
  - `[决策]` 不可覆盖，只追加新版本或冲正；
  - `[配置]` 种子/参数，可 upsert；
  - `[运行]` 运行与审计日志，append-only。
- **时间**：时间戳一律 UTC TEXT（ISO8601）；`trade_date`/`week_end_date`/`observed_on`/`ex_date` 等为**市场本地日期** TEXT（YYYY-MM-DD）。
- **数值口径**：关键决策值（卡片价区、执行价/量/费、汇率、财务金额/股数）存 **TEXT 定点十进制字符串**；行情 OHLCV、指标等中间/展示值允许 REAL（§9.5 软约束）。
- **复权口径**（§3.3/§4.1）：`daily_bars` 存不复权价 + `price_adj_factor`（前向累积因子，origin 日=1.0）与 `share_factor`（只反映股数变化）；派生层复权价 = raw × price_adj_factor，调整量 = volume_raw ÷ share_factor。卡片/现价比较一律不复权口径。
- **JSON 列**一律 TEXT，写库前按对应 JSON Schema 校验。
- 审计字段：`run_id`（ pipeline_runs 外键语义）、`rule_version`、`config_hash`（参数文件内容 sha256）。

## 2. 表清单速查

| 表 | 生命周期 | 主键 | 用途 | 写入方 |
|---|---|---|---|---|
| schema_migrations | [运行] | version | 已应用 migration 记录 | pipeline/db.py |
| pipeline_runs | [运行] | (run_id, stage) | 各阶段运行记录与版本审计 | 所有管线/信号模块 |
| raw_objects | [原始] | raw_object_id | 来源落盘文件索引 + content hash 去重 | adapters/common.py |
| data_revisions | [原始] | revision_id | 事实表修订前后值（第一版日志式） | adapters/common.py |
| watchlist | [配置] | symbol | 股票池（种子 config/watchlist.yaml） | pipeline/db.py |
| trading_calendar | [事实] | (market, trade_date) | 逐日交易日历（种子 calendar_*.yaml） | pipeline/db.py |
| daily_bars | [事实] | (symbol, trade_date) | 不复权日线 + 复权因子 | adapters/stock_finance_data.py；因子由 pipeline/adjust.py UPDATE |
| corporate_actions | [事实] | ca_id；UNIQUE(symbol, ex_date, action_type) | 除权除息/拆并股事件 | adapters/yahoo_finance.py |
| adjustment_factor_versions | [事实] | version_id | 因子重建版本（origin/方向/算法） | pipeline/adjust.py |
| weekly_bars | [派生] | (symbol, week_end_date) | 完成周复权周线 | pipeline/weekly.py |
| index_bars | [事实] | (index_code, trade_date) | 基准指数日线（日历交叉校验） | adapters/stock_finance_data.py |
| events | [事实] | event_id | 公告/新闻事件事实（不含评价） | adapters/stock_finance_data.py, adapters/tianyancha.py |
| event_symbols | [事实] | (event_id, symbol) | 事件-股票关联 | adapters/stock_finance_data.py |
| event_assessments | [决策] | (event_id, assessment_version) | LLM 消息评价版本（未接入） | —（D3 预留） |
| financial_reports | [事实] | report_id；UNIQUE(symbol, period_end, period_type, is_cumulative, revision) | 财报头（修订新增 revision） | adapters/stock_finance_data.py |
| financial_facts | [事实] | report_id（引用） | 财务事实（营收/归母净利/EPS/股本） | adapters/stock_finance_data.py |
| share_capital_events | [事实] | sce_id | 股本变动事件/快照 | indicators/valuation.py |
| fx_rates | [事实] | (from, to, rate_date) | 财务币种→交易币种日汇率 | adapters/yahoo_finance.py |
| forecasts | [事实] | snapshot_id | 分析师预测历史快照 | adapters/stock_finance_data.py |
| indicators_daily | [派生] | (symbol, trade_date) | 日线指标全套 + PE(TTM) | indicators/compute.py |
| indicators_weekly | [派生] | (symbol, week_end_date) | 周线指标（只完成周） | indicators/compute.py |
| weekly_anchors | [派生] | anchor_id | 周线锚点（恐慌低点/下跌起点） | signals/weekly_signals.py |
| signal_facts | [派生] | fact_id；UNIQUE(symbol, signal, observed_on) | 全部确定性信号事实 | signals/{weekly_signals, daily_watch, right_side, accumulation, corporate_action}.py |
| strategy_card_versions | [决策] | card_version_id | 排期卡不可变版本 | pipeline/card.py、signals/corporate_action.py（换算 draft） |
| executions | [决策] | execution_id；UNIQUE idempotency_key | 执行记录（冲正不删改） | pipeline/execution.py |
| report_runs | [输出] | report_run_id | 报告运行记录（revision 递增） | pipeline/report.py |

## 3. 运行与审计

### pipeline_runs — 阶段运行记录 [运行]

每次管线/信号运行按 (run_id, stage) 一行；同 run_id+stage 重跑覆盖（幂等）。

| 字段 | 类型 | 说明 |
|---|---|---|
| run_id | TEXT | 运行标识，如 `daily_2026-08-07`、`weekly_signals_603605.SH_...` |
| stage | TEXT | 阶段名：`calendar` / `symbol:{symbol}` / `report` / `summary` / `weekly_signals` / `daily_watch` / `accumulation` / `adjust` 等 |
| as_of | TEXT | 本次计算的数据截止时间（UTC） |
| data_cutoff | TEXT | 入库数据截止（市场本地日期或 UTC） |
| adapter_version / app_version / git_commit | TEXT | 版本三元组（§2.3） |
| config_hash | TEXT | 参数文件内容 sha256 |
| rule_version | TEXT | 规则版本，如 `indicators_v1` / `signals_v1` / `report_v1` |
| card_version_id | TEXT | 涉及卡片时记录 |
| status | TEXT | running / success / degraded / failed |
| error | TEXT | 失败/降级原因 |
| started_at / finished_at | TEXT | UTC |

### raw_objects — 来源文件索引 [原始]

| 字段 | 类型 | 说明 |
|---|---|---|
| raw_object_id | TEXT PK | 来源文件标识 |
| run_id | TEXT | 采集批次 |
| source | TEXT | stock_finance_data / yahoo_finance / tianyancha / ... |
| data_type | TEXT | price / announcement / fx / stock_actions / financials / forecast / ... |
| symbol | TEXT | 标的（指数/汇率可为空） |
| request_params_json | TEXT | 请求参数 JSON |
| file_path | TEXT | data/raw/{source}/{data_type}/{date}/{run_id}/... |
| content_hash | TEXT | 相同 hash 不重复解析（§8.3 幂等；索引 idx_raw_objects_hash） |
| fetch_status | TEXT | ok / error |
| ingested_at | TEXT | UTC |

### data_revisions — 事实修订日志 [原始]

来源修订规范化事实时记录前后值（§9.5 第一版降级为日志式）。字段：table_name、record_key_json（主键定位）、field_name（空=整行重建）、old_value、new_value、source、reason、run_id、created_at。

### schema_migrations — migration 记录 [运行]

`db.migrate()` 维护：`version`、`name`、`applied_at`。新增表/改表须新增 migrations/NNNN_*.sql，不得改已应用文件。

## 4. 配置与日历

### watchlist — 股票池 [配置]

种子 `config/watchlist.yaml`（db.py 启动时 upsert）。

| 字段 | 说明 |
|---|---|
| symbol | PK，如 `603605.SH` |
| market | CN / HK |
| name / aliases_json | 名称与新闻匹配别名 |
| benchmark_code | 基准指数（000300.SH / ^HSI，可按股票覆盖，§3.5） |
| currency / timezone | 交易币种 / 市场本地时区 |
| active | 0/1，每日管线只处理 active=1 |

### trading_calendar — 交易日历 [事实]

种子 `config/calendar_{market}_{year}.yaml` 展开为逐日行（§3.5：指数不能作为唯一日历来源）。主键 (market, trade_date)。字段：is_open、is_full_day（半日市=0）、session_open/close（市场本地）、status（trading/half_day/weekend/holiday）、status_detail（节假日名）、timezone、source（种子文件名）、updated_at。**已知缺口：HK 日历未填充。**

## 5. 行情与公司行为

### daily_bars — 不复权日线 + 复权因子 [事实]

| 字段 | 类型 | 说明 |
|---|---|---|
| symbol / trade_date | TEXT | PK |
| market | TEXT | CN / HK |
| open_raw/high_raw/low_raw/close_raw | REAL | 不复权 OHLC |
| volume_raw | REAL | 成交量（股） |
| amount_raw | REAL | 成交额，可为空（stock_finance_data 无 amount，见 probe 记录；amt_* 指标因此为 NULL） |
| currency | TEXT | |
| price_adj_factor | REAL | 前向累积复权因子，origin 日归一 1.0；复权价 = raw × factor（§3.3）。由 adjust.py 平台段检测写入/重建 |
| share_factor | REAL | 只反映拆股/送转等股数变化；调整量 = volume_raw ÷ share_factor |
| trading_status | TEXT | normal / suspended |
| source / raw_object_id / updated_at | | 来源追溯 |

来源修订走 upsert + data_revisions；因子变化时 adjust.py 全量重建该股因子并同事务重建周线（adjustment_factor_versions 记版本）。

### weekly_bars — 完成周复权周线 [派生]

只写**完成周**（由 trading_calendar 判定；当周未结束不写）。主键 (symbol, week_end_date)。字段：week_start_date、open_adj/high_adj/low_adj/close_adj（逐日复权后聚合）、volume_adj（调整量求和）、amount_raw（成交额求和，不做股份调整）、trading_days、run_id。可整体 DELETE 重算（pipeline/weekly.py）。

### index_bars — 基准指数日线 [事实]

主键 (index_code, trade_date)：000300.SH / ^HSI。OHLCV + currency + source + available_at。用途：日历交叉校验（calendar_check）与超额收益对照。

### corporate_actions — 公司行为事件 [事实]

UNIQUE(symbol, ex_date, action_type)。字段：ex_date（除权除息生效日，市场本地）、action_type（cash_dividend / split / bonus_share / ...）、cash_per_share（每股分红，定点 TEXT）、split_ratio（股份倍率，定点 TEXT）、details_json（来源组合）、source、available_at、raw_object_id。**§5.4b 冻结/换算的输入**：生效卡片遇因子变化 → 冻结触发 → 机械换算 draft → 人工确认。

### adjustment_factor_versions — 因子版本 [事实]

每次因子重建一行：factor_origin_date（归一化日，该日因子=1.0）、direction（forward_cumulative）、algorithm（如 forward/none 平台段检测）、source、run_id、notes。索引 (symbol, created_at)。

## 6. 事件与消息

### events — 公告/新闻事实 [事实]

只存事实字段，不混 LLM 评价（§3.6）。event_id PK；event_type（announcement/news）；三时点（§2.1）：event_at（实际发生，可空）、published_at + published_tz（来源发布）、available_at（系统允许参与计算的最早时间）；title/summary/canonical_url；去重：source_external_id 优先，content_hash 其次。索引 published_at、(source, source_external_id)。

### event_symbols — 事件-股票关联 [事实]

(event_id, symbol) 复合主键，一条事件可关联多股。

### event_assessments — LLM 消息评价 [决策]

(event_id, assessment_version) 版本化，不覆盖（§5.5）。字段：model、prompt_version、assessed_at、direction（positive/negative/neutral）、materiality、confidence、rationale、status（ok/needs_review/degraded）、event_study_json（T+1/T+5 事件研究）。**LLM 评价（D3）仍未接入**；2026-08-14 起由 `scripts/signals/event_study.py` 写入确定性事件研究行（assessment_version='event_study_v1'、model='deterministic'，direction/materiality/confidence/rationale 置 NULL 不冒充评价，status 取值 ok/suspended/degraded，event_study_json 含 base/t1/t5 明细与 pending/suspended 标记）。

## 7. 财务、股本、预测与汇率

### financial_reports — 财报头 [事实]

修订新增 revision 不覆盖旧版，UNIQUE(symbol, period_end, period_type, is_cumulative, revision)。字段：period_end（报告期截止）、period_type（annual/interim/quarterly）、fiscal_year、published_at + published_tz（正式披露，**当前来源缺失 → available_at 降级取报告期截止，pe_status 带 degraded_available_at 标注**）、available_at（§2.1：取披露时间不取报告期截止）、currency、unit、is_cumulative（季报/中报为累计值）。

### financial_facts — 财务事实 [事实]

report_id 引用 financial_reports。金额/股数为关键决策值存 TEXT 定点：revenue、net_profit_attr（归母净利，TTM 与 PE 口径）、eps_basic/eps_diluted、shares_issued_end（期末已发行股数，PE 默认口径）、shares_float_end、share_count_type（issued/float；来源只有流通股数时必须标注，§3.7）。

### share_capital_events — 股本变动 [事实]

字段：effective_at（生效日）、available_at、event_type（issuance / buyback_cancel / bonus_share / conversion / snapshot_issued（yahoo 快照）/ snapshot_group_total（stock_finance_data 快照）——当前数据均为单点快照）、share_change、shares_issued_after、share_count_type（issued=已发行股数（A/H 双上市公司的 yahoo 快照实际只含 A 股）/ float=流通股 / group_total=A+H 集团总股本，vendor 通用 PE 股本口径）、details_json（§3.7 三来源优先级标注 + 单点快照假设）、source。PE 股本口径取 effective_at ≤ 计算日最新记录，同一 effective_at 多口径并存时优先 group_total、回退 issued（2026-08-17 起 13 只均有 group_total 快照，详见执行日志当日条目）。

### fx_rates — 汇率 [事实]

(from_currency, to_currency, rate_date) 主键：财务币种→交易币种日汇率，rate 定点 TEXT。港股财报换算必需；A 股 CNY→CNY 不依赖。

### forecasts — 预测快照 [事实]

每次抓取全量保存（snapshot_at UTC）；历史查询取 snapshot_at ≤ as_of 最新快照（点时语义 §2.1）。payload_json 为当次预测全量（FY1–FY3 净利/营收/增速）。

## 8. 指标（派生，可重算）

### indicators_daily — 日线指标 [派生]

主键 (symbol, trade_date)。全部指标基于**复权价 + 调整量**（§4.1，公式由 tests/test_indicators.py golden tests 锁定）：

| 字段组 | 字段 | 口径 |
|---|---|---|
| 均线 | ma5/10/20/60/120/250 | 简单移动平均，窗口不足为 NULL |
| MACD | dif/dea/macd_hist | EMA(12,26,9) adjust=False，柱=2×(DIF−DEA) |
| RSI | rsi6/12/24 | Wilder RMA |
| BOLL | boll_mid/upper/lower/bandwidth | mid=MA20，±2σ（ddof=0），带宽=(上−下)/中 |
| 量能 | vol_ma5/10、vol_mean20/std20、vol_mean60/std60 | 调整量（÷share_factor）均值/总体标准差 |
| 成交额 | amt_mean20/std20、amt_mean60/std60 | 来源缺 amount → 全 NULL（已知缺口） |
| KDJ | kdj_k/d/j | RSV(9)，K/D 初值 50 平滑，J=3K−2D |
| 基础量 | pct_chg / amplitude | 百分比存储（1.23 = 1.23%） |
| 估值 | pe_ttm / pe_status | 不复权市值 ÷ TTM 归母净利；pe_status 为空值/降级原因码 |
| 审计 | run_id / rule_version / config_hash / computed_at | |

⚠️ 展示时与不复权现价比较必须 ÷ 当日 price_adj_factor 折回（§5.4；报告指标快照已带折回行）。

### indicators_weekly — 周线指标 [派生]

主键 (symbol, week_end_date)，只完成周。字段为 indicators_daily 子集（MA5–60、MACD、RSI、BOLL、vol_*20、KDJ、pct_chg、amplitude + 审计字段），底背离信号用 rsi12/macd_hist。

## 9. 信号（派生，可重算）

### weekly_anchors — 周线锚点 [派生]

| 字段 | 说明 |
|---|---|
| anchor_id | 自增 PK；锚点身份变化追加新 id，不覆盖旧行（§5.2） |
| as_of | 识别时点（该完成周周末日） |
| anchor_type | panic_low（恐慌低点）/ decline_start（下跌起点） |
| trade_date | 锚点交易日（周内定位） |
| adjusted_price | 识别时复权价（技术比较用） |
| raw_price | 当日不复权价（排期卡价区比较用，§3.4） |
| is_fallback | 0/1，fallback 锚点（26 周最低收盘）标 1 |

### signal_facts — 信号事实总表 [派生]

全部确定性信号的统一存储（§5.1），UNIQUE(symbol, signal, observed_on)。

| 字段 | 说明 |
|---|---|
| observed_on | 观测日（日频信号）/ 观测周周末日（周线信号），市场本地 |
| signal | 信号类型，见下表 |
| state | active / inactive / watching / triggered / pending_signals / suspended / incomplete / 状态机状态（idle/consolidating/confirmed/failed 等） |
| anchor_id | 关联 weekly_anchors；同一 anchor_id 下统计活跃衰竭信号数（释放二、三档的"≥2 项"口径） |
| triggered | 0/1，本次观测是否触发/状态转换 |
| active_until | 活跃截止（市场本地日期，可日历外推） |
| details_json | 参与判断的日期、原值、阈值、锚点、原因码（无未来函数审计依据） |
| run_id / rule_version / config_hash / created_at | 审计 |

signal 取值与写入模块：

| signal | 模块 | 粒度 | 说明 |
|---|---|---|---|
| panic / dry_up / no_new_low_3w / divergence / duration | weekly_signals | 周 | 五项衰竭信号（§5.3） |
| daily_watch / tier_proximity / tier_triggered / falsification_breach / box_position / ma_comparison | daily_watch | 日 | 日频监测（§5.4；无 active 卡只写 incomplete 行） |
| right_side | right_side | 日 | 右侧确认状态机 |
| accumulation | accumulation | 日 | 吸筹形态状态机（§5.4c，仅观察点） |
| corporate_action_freeze / 换算相关 | corporate_action | 日 | §5.4b 冻结与换算事实 |

重算语义：各模块 DELETE 自己管理的 signal 行后全量重插，同事务；历史决策依据由 executions.signal_snapshot_json / report_runs.input_snapshot_json 冻结，不依赖本表重算结果。

## 10. 决策与执行

### strategy_card_versions — 排期卡版本 [决策]

不可变版本（§5.6）：LLM/Skill 只产 draft，人工确认激活；激活新版关闭旧版 effective_to，旧版不修改。**硬门槛：同一股票同一时刻最多一个 active（部分唯一索引 uq_card_active）。**

| 字段 | 说明 |
|---|---|
| card_version_id | PK，如 `603605SH_120ca661` |
| status | draft / active / superseded / rejected |
| schema_version | 卡片 JSON schema 版本（card_v1） |
| effective_from / effective_to | 生效区间（市场本地日期，开口=NULL） |
| supersedes_id | 指向被替代版本（换算链/修订链追溯） |
| currency / price_basis | 价区口径（第一版=不复权绝对价位） |
| earnings_scenarios_json | EPS 三情景（定点字符串） |
| valuation_scenarios_json | PE 刻度/情景（须标注 3 年样本区间，§3.2） |
| price_tiers_json | 三档价区 `[{tier, zone_low, zone_high}]`（关键决策值） |
| invalidation_json | 证伪线 `{line}` |
| swing_box_json | 波段箱体（box_low/high、buy_zone、sell_zone、box_invalidation、仓位上限） |
| right_side_trigger_json | 右侧触发位/止损位 |
| next_review_at | 复核到期日（到期生成提醒，不自动延后） |
| input_snapshot_json | 输入快照/换算来源明细（§5.4b） |

### executions — 执行记录 [决策]

append-only；错录用冲正（action_type=reversal + reverses_execution_id）修复，不更新/删除原记录（§5.7）。

| 字段 | 说明 |
|---|---|
| idempotency_key | UNIQUE；显式键或派生键防重复录入 |
| executed_at | UTC（缺省取当前；--backfill 补录历史单关联当前 active 卡） |
| action_type | buy / sell / reversal |
| tier / price / quantity / fees | 档位；价/量/费定点 TEXT |
| card_version_id | 执行时生效卡片（审计沿 supersedes 链重现） |
| signal_snapshot_json | 执行时信号/指标快照，冻结不随重算变化 |

## 11. 输出

### report_runs — 报告运行记录 [输出]

已发布报告不被重算覆盖；同日重跑 = 新 revision 行 + 覆盖同名文件（§9.5 降级）。字段：report_type（single/daily/weekly）、symbol（全池日报为空）、trade_date、revision、card_version_id、rule_version、config_hash、input_snapshot_json（报告输入快照，决策点可追溯 §9.3）、status（complete/incomplete/degraded/failed）、file_path、run_id。

## 12. 关键引用关系

```
raw_objects ──< daily_bars / index_bars / events / financial_reports / share_capital_events / fx_rates / corporate_actions / forecasts
financial_reports ──< financial_facts
events ──< event_symbols、event_assessments
weekly_anchors ──< signal_facts.anchor_id（同锚点活跃信号计数）
strategy_card_versions ──< strategy_card_versions.supersedes_id（自引用版本链）
strategy_card_versions ──< executions、report_runs、pipeline_runs（card_version_id）
executions ──< executions.reverses_execution_id（冲正链，自引用）
```

日常查询入口建议：先 `pipeline_runs` 看运行状态 → `signal_facts`（details_json 含全部阈值/原值/原因码）→ 必要时回 `daily_bars`/`weekly_bars` 与 `indicators_*` 核对口径（注意复权 vs 不复权）。
