# 实现计划

> 依据 `docs/system_design.md`（实现基线 v2）。上线顺序遵循 9.4；硬门槛/软约束分级见 9.5。
> 执行进展记录在 `docs/execution_log.md`。

## 阶段 D0：项目骨架

| # | 任务 | 产出 | 完成判据 |
|---|---|---|---|
| 0.1 | uv 项目初始化 | `pyproject.toml`、`.venv` | `uv run python -V` 可用；依赖：pandas、pyyaml、jsonschema、pytest（下载失败走系统代理） |
| 0.2 | 目录结构 | `scripts/{adapters,pipeline,indicators,signals}`、`config/`、`data/raw/`、`tests/`、`cards/`、`reports/` | 与设计文档 §7 目录一致 |
| 0.3 | 参数配置 | `config/indicators.yaml`、`config/signals.yaml`（含 defaults 段，无 overrides）、`config/calendar_cn_2026.yaml`、`config/calendar_hk_2026.yaml` | 参数覆盖设计 §4.1/§5.2-5.4 全部默认值 |
| 0.4 | watchlist 种子 | 珀莱雅 603605.SH（benchmark 000300.SH）入 `watchlist` | 可查询 |
| 0.5 | 采集 Skill | `skills/stock-collect/`：调 MCP 数据源 → 落盘 `data/raw/`，只搬运 | skill 可被调用并产出 raw 文件 |

## 阶段 D1：行情、日历、复权、指标、质量门禁

| # | 任务 | 产出 | 完成判据（对应验收 §9.1） |
|---|---|---|---|
| 1.1 | 插件实测 | `data/raw/` 下珀莱雅 3 年日线 CSV + 字段勘察记录 | 明确 amount/复权字段有无，定 adapter 映射 |
| 1.2 | SQLite schema v1 + migration | `scripts/pipeline/db.py`、migration 0001 | 建齐 D1 所需表（见下） |
| 1.3 | adapters | `adapters/stock_finance_data.py`（行情/财报/公告/预期）、`adapters/yahoo_finance.py`（港股行情、FX、stock_actions）、指数行情 | raw → 规范化事实入事务库；重复 content hash 不重复解析 |
| 1.4 | 交易日历 | 种子导入 + 指数交叉校验 | 停牌/缺数/非交易日可区分，冲突输出 incomplete |
| 1.5 | 复权因子 | `pipeline/adjust.py`：来源因子 → 前向累积因子（固定归一化日）；重叠窗口发现因子变化 → 全量重建 | golden tests 通过；除权周中日线连续 |
| 1.6 | 周线聚合 | `pipeline/weekly.py`：逐日复权后聚合，只写完结周 | golden tests：周中除权周 K 不扭曲 |
| 1.7 | 指标模块 | `indicators/` 全套（MA/MACD/RSI/BOLL/KDJ/量能/pct_chg/amplitude）+ TTM + pe_ttm（市值口径） | golden tests 锁定公式边界；窗口不足返回空 |
| 1.8 | 数据质量门禁 + pipeline 骨架 | `pipeline/daily.py`：阶段编排、事务、幂等、incomplete 输出 | 关键数据缺失时不产出"条件满足" |

D1 涉及表：`pipeline_runs`、`raw_objects`、`watchlist`、`trading_calendar`、`daily_bars`、`corporate_actions`、`adjustment_factor_versions`、`weekly_bars`、`index_bars`、`financial_reports`、`financial_facts`、`share_capital_events`、`fx_rates`、`forecasts`、`indicators_daily`、`indicators_weekly`。

## 阶段 D2：信号、卡片、报告

| # | 任务 | 产出 | 完成判据（对应 §9.2/9.3） |
|---|---|---|---|
| 2.1 | 周线锚点 + 衰竭信号 | `signals/anchors.py`、`signals/exhaustion.py`（5 项 + episode 生命周期 + active_until） | 阈值边界单测（等于/略高/略低）；底背离只在 pivot 确认日出现 |
| 2.2 | 日频监测 | `signals/daily_watch.py`：证伪线（2 日 1%）、档位触发/临近（3%）；口径换算纪律（复权均线 ÷ 当日因子） | 跨尺度对比有测试 |
| 2.3 | 右侧状态机 | `signals/right_side.py`：idle→waiting_retest→confirmed/invalidated/expired | 三条路径测试通过 |
| 2.4 | 公司行为处置 | `signals/corporate_action.py`：分红快速通道（<2% 自动换算激活）+ 三段式（冻结/换算 draft/确认） | §9.1 除权验收条通过 |
| 2.5 | 卡片版本 + 执行记录 | `strategy_card_versions`、卡片 Markdown 渲染、确认激活 CLI、`executions`（append-only + 冲正） | 同时刻最多一个 active；冲正不改历史 |
| 2.6 | 报告生成器 | `pipeline/report.py`：单股报告（§6.2 七段）+ 全池日报（§6.3 排序）+ `report_runs` 快照 | 决策点可追溯到卡片版本/信号明细/config_hash |

## 阶段 D3：验证

| # | 任务 | 产出 | 完成判据 |
|---|---|---|---|
| 3.1 | 构造样本测试 | 周中除权、停牌、财报更正、因子突变四场景 fixtures | §9.1 全部通过 |
| 3.2 | 端到端跑通 | 珀莱雅 3 年真实数据全 pipeline | 日报可生成，无伪触发 |
| 3.3 | 排期卡 skill 适配 | `skills/fred-valuation-card-skill/` 数据源 iFinD→kimi-datasource；生成首张卡并人工激活 | 卡片字段完整入库，标注 3 年样本区间 |
| 3.4 | 并行观察 | 数周人工核对锚点/信号明细，记录于 execution_log | 形成调参建议（不改默认值） |

## 阶段 D4：LLM 接入

| # | 任务 | 产出 | 完成判据 |
|---|---|---|---|
| 4.1 | 消息 adapters | 新浪历史页解析器、东财搜索、披露易 | 去重（来源 ID > URL > content hash）正确 |
| 4.2 | 消息评价 | `event_assessments` + JSON Schema + 评价 Skill（prompt 版本管理） | 评价版本化；失败标 degraded 不阻断 |
| 4.3 | event study | T+1/T+5 超额收益（保守时点，benchmark 对照） | 停牌标 suspended；描述性表述 |
| 4.4 | 胜率重估 + draft 自动生成 | 接入 pipeline 周度节奏 | D1–D3 验收全部通过后才启用 |

## 依赖关系

- D1 内部：1.1 → 1.2 → 1.3 →（1.4、1.5）→ 1.6 → 1.7 → 1.8。
- D2 依赖 D1 的指标与门禁；2.5 卡片是 2.2/2.4 的输入（监测需要 active 卡片），首张卡可先手工录入简化版。
- D3.3（首张卡）可在 D2.5 完成后提前穿插。
- D4 全部依赖 D3 通过（设计 9.4 硬约束）。

## 实施约定

- 代码形态：`scripts/` 为可 `python -m` 调用的包，便于测试与 cron。
- 环境：uv 管理；包下载失败时使用系统代理重试。
- 每个阶段完成后更新 `docs/execution_log.md`（日期、完成项、偏差、下一步）。
