# 个人股票监测系统

> 一个面向左侧交易的个人股票监测系统。核心原则：**确定性管线在 Python/SQLite 中完成，LLM 只消费结构化底稿、只产出排期卡 draft，不生成规范化数字，也不直接激活卡片。**

---

## 1. 项目定位

- **目标**：对自选池（watchlist）股票做日线/周线分析，按估值排期卡每日盘后输出观察点和决策点，保存可审计的历史判断依据。
- **策略内核**：估值排期卡框架——估值锚定档（赔率）+ 胜率打分（仓位）+ 衰竭信号择时（时机）+ 证伪线兜底（认错）。
- **非目标**：不预测涨跌、不接券商下单、不做组合级自动优化。

---

## 2. 技术栈与运行依赖

- **Python** ≥ 3.12，使用 **uv** 管理依赖
- 主依赖见 `pyproject.toml`：`pandas`、`pyyaml`、`jsonschema`、`flask`（只读 Web UI）
- 测试：`pytest`
- 数据库：SQLite `data/market.db`（派生库，可由 `data/raw/` 重跑重建）
- 可选数据源：`akshare`（`uv sync --extra akshare` 后使用）

---

## 3. 环境安装

### 3.1 初次创建环境

```bash
uv sync
# 如需 akshare 数据源：
uv sync --extra akshare
```

### 3.2 建库与种子

```bash
# 创建表结构和交易日历、watchlist 等种子
uv run python -m scripts.pipeline.db migrate
uv run python -m scripts.pipeline.db seed
```

### 3.3 验证

```bash
uv run pytest -q
# 当前 559 项测试应全绿
```

---

## 4. 数据管线总览

数据从来源采集到报告生成，经过以下环节，每个环节都是幂等的，可重复重跑：

```
采集（akshare_collect 每日主源；tdx-collect / stock-collect Skill 备选）
  → 落盘 data/raw/{source}/{data_type}/
  → adapter 解析/校验/入库（scripts/adapters/）
  → 复权因子计算（scripts/pipeline/adjust.py）
  → 周线聚合（scripts/pipeline/weekly.py）
  → 指标计算（scripts/indicators/compute.py）
  → 周线衰竭信号（scripts/signals/weekly_signals.py）
  → 日频监测（scripts/signals/daily_watch.py）
  → 右侧确认状态机（scripts/signals/right_side.py）
  → 吸筹形态（scripts/signals/accumulation.py）
  → 公司行为处置（scripts/signals/corporate_action.py）
  → 排期卡管理（scripts/pipeline/card.py）
  → 报告生成（scripts/pipeline/report.py，含日报"模拟盘"段）
  → [可选观察项] 自算筹码分布（scripts/indicators/chip_distribution.py）
  → [模拟盘] 决策录入/结算/统计（scripts/paper/）
```

**每日例行主入口**（盘后先采集再跑管线）：

```bash
uv run python -m scripts.collect.akshare_collect \
  --sources price,forward,financials,index,telegraph,announcement,macro,flow \
  --start 2023-08-01 --end 2026-09-05 --date 2026-09-05 \
  --price-api sina --run-id run_daily_20260905
uv run python -m scripts.pipeline.daily --date 2026-09-05 --raw-dir data/raw/akshare
```

> ⚠️ akshare 采集 `--end` 默认值不随日期更新，**必须显式传当日**；price 主用 sina 源
> （东财 push2his 域名有间歇性风控）。

---

## 5. 逐步使用指南

### 5.1 维护观察池

编辑 `config/watchlist.yaml` 添加股票，随后用对应采集方式初始化或增量。

---

### 5.2 数据采集

#### A. 通达信 tdx-collect Skill（第一优先级数据源）

**使用智能体**：调用 `tdx-collect` Skill 的 Agent 或 MCP 环境。

功能：
- A 股/港股/指数日 K
- 估值/股本快照
- 公告
- 落盘到 `data/raw/tdx/`

采集约定见 `skills/tdx-collect/SKILL.md`。

#### B. stock-collect Skill（kimi-datasource，fallback）

**使用智能体**：调用 `stock-collect` Skill。

- A 股行情/财报/公告/一致预期
- 港股行情、公司行为、指数
- 落盘到 `data/raw/stock_finance_data/` 或 `data/raw/yahoo_finance/`

采集约定见 `skills/stock-collect/SKILL.md`。

#### C. akshare 采集器（每日主源，纯 Python）

akshare 是 Python 第三方库，**不需要调用智能体/Skill**，直接跑脚本：

```bash
# 先安装可选依赖
uv sync --extra akshare

# 每日增量（八源，显式 --end 当日 + sina 价格源）
uv run python -m scripts.collect.akshare_collect \
  --sources price,forward,financials,index,telegraph,announcement,macro,flow \
  --start 2023-08-01 --end 2026-09-05 --date 2026-09-05 \
  --price-api sina --run-id run_daily_20260905
```

支持 sources：`price`（含换手率列）、`forward`、`financials`、`index`、`telegraph`、
`forecast`、`stock_info`、`announcement`（巨潮公告）、`macro`（商品/外汇宏观因子）、
`flow`（龙虎榜/大宗）、`gdhs`（股东户数，手触发）、`balance_sheet`/`cash_flow`/
`fin_abstract`（财务三表，手触发）、`calendar`（披露预约）、`industry`（行业归属）。

> 踩坑提示：①`--end` 必须显式；②新入池首采后核对各源 CSV 实际落盘；③东财域名
> （push2his/emweb）间歇性 SSL 抽风，冷却重试即可。

---

### 5.3 数据入库

#### 通用 ingester（所有来源）

```bash
# 按 data/raw/{source}/{data_type}/ 路由解析入库
uv run python -m scripts.pipeline.ingest data/raw/tdx/2026-08-27
```

#### 每日管线（推荐）

```bash
uv run python -m scripts.pipeline.daily --date 2026-08-27 --raw-dir data/raw/tdx/2026-08-27
```

会依次完成：
1. raw 文件解析入库
2. 交易日历/停牌门禁
3. 复权因子检查/重建
4. 周线/指标/信号重算
5. 日报生成

支持状态查询：

```bash
uv run python -m scripts.pipeline.daily status 603605.SH
```

---

### 5.4 单独重跑各环节

用于数据补齐、参数调整后的局部重算（全部幂等）：

```bash
# 复权因子 + 周线
uv run python -m scripts.pipeline.adjust 603605.SH
uv run python -m scripts.pipeline.weekly 603605.SH

# 指标
uv run python -m scripts.indicators.compute 603605.SH

# 信号
uv run python -m scripts.signals.weekly_signals 603605.SH
uv run python -m scripts.signals.daily_watch 603605.SH
uv run python -m scripts.signals.right_side 603605.SH
uv run python -m scripts.signals.accumulation 603605.SH
uv run python -m scripts.signals.corporate_action 603605.SH
```

---

### 5.5 生成排期卡（核心）

#### 步骤 1：导出系统底稿

```bash
uv run python -m scripts.pipeline.card_inputs 603605.SH
# → cards/603605.SH/inputs_2026-08-27.json
```

#### 步骤 2：使用 fred-valuation-card-skill 生成 draft

**使用智能体**：调用 `fred-valuation-card-skill` Skill 的 Agent。

该 Skill 消费 `inputs_*.json` 底稿，执行 9 步流程：
1. 取数
2. 盈利底稿
3. 历史底部估值刻度
4. 情景矩阵与三档排期
5. 衰竭信号规则
6. 胜率打分
7. 波段仓与右侧确认仓规则
8. 锚维护日历
9. 输出排期卡 Markdown + 卡片 JSON

输出物放在 `cards/{symbol}/`，包括：
- `{股票名}估值排期卡_draft_{YYYY-MM-DD}.md`
- `draft_{YYYY-MM-DD}.json`

#### 步骤 3：人工校验并入库

```bash
# 结构校验，写入 draft
uv run python -m scripts.pipeline.card create-draft 603605.SH --json cards/603605.SH/draft_2026-08-27.json

# 人工确认后激活
uv run python -m scripts.pipeline.card activate 603605SH_xxxx --effective-from 2026-08-27

# 或拒绝/废止
uv run python -m scripts.pipeline.card reject 603605SH_xxxx
```

> **重要约定**：LLM/Skill 只能产出 draft，不能自行 `activate`/`reject`，必须由人工确认。

---

### 5.6 执行记录

每笔实际交易必须关联当前 active 卡片：

```bash
uv run python -m scripts.pipeline.execution add \
  --symbol 603605.SH \
  --action buy \
  --price 55.00 \
  --quantity 1000 \
  --fees 5.50 \
  --tier 1 \
  --executed-at 2026-08-27

# 补录历史手工单
uv run python -m scripts.pipeline.execution add ... --backfill --note "手工单"

# 冲正
uv run python -m scripts.pipeline.execution reverse <execution_id>
```

---

### 5.7 查看报告

#### 单股报告

```bash
uv run python -m scripts.pipeline.report --date 2026-08-27 --symbol 603605.SH
# → reports/603605.SH/2026-08-27.md
```

#### 全池日报

```bash
uv run python -m scripts.pipeline.report --date 2026-08-27
# → reports/daily/2026-08-27.md
```

---

### 5.8 启动 Web UI

```bash
uv run python -m scripts.ui.app
# http://127.0.0.1:5000/
# 探活 http://127.0.0.1:5000/health
```

默认端口 5000，可覆盖：

```bash
uv run python -m scripts.ui.app --port 5001
```

页面入口：
- `/`：股票启动台
- `/stock/{symbol}`：单股三图联动分析页
- `/data`：信号/指标/对比/卡片/运行记录汇总入口

---

### 5.9 模拟盘（信号决策力实验）

在真实买卖之外，对每个信号触发点人工录"跟 / 不跟 / 看反"决策，收盘价成交、对称退出，
统计**判断力差值 = 主观组合收益 − 机械基线收益**（同决策点集"信号即全跟"）。与
`executions` 完全隔离。设计见 `docs/superpowers/specs/2026-09-05-paper-trading-design.md`。

```bash
# 列待决策点（来源=当日触发信号，带编号）
uv run python -m scripts.paper.decide --pending

# 录决策（follow 跟 / skip 不跟 / counter 看反；成交价系统取当日收盘，不可自填）
uv run python -m scripts.paper.decide --pick 25 --decision follow --note "备注"

# 兜底结算扫描 / 人工平仓 / 冲正
uv run python -m scripts.paper.settle run
uv run python -m scripts.paper.settle manual-close --symbol 603605.SH --reason "..."
uv run python -m scripts.paper.reversal --id 12 --reason "录错了"

# 统计（follow/skip/counter 三组 + 机械基线 + 差值；样本<30 仅描述统计）
uv run python -m scripts.paper.stats [--exclude-late]
```

全池日报末尾有"模拟盘"段（当日待决/浮盈/累计）；反作弊：价格系统取、T+1 录入窗口
（超窗标 late）、append-only 冲正、快照冻结。

---

### 5.10 自算筹码分布与扩展数据

```bash
# 自算筹码分布（换手率衰减模型；获利比例/平均成本/90%成本区间/集中度；
# 模型估算观察项，不进信号链；adjust 因子重建后需重算）
uv run python -m scripts.indicators.chip_distribution 603605.SH
uv run python -m scripts.indicators.chip_distribution --all

# 股东户数（东财，手触发；换手率已随 price 源自动采集）
uv run python -m scripts.collect.akshare_collect --sources gdhs \
  --date 2026-09-05 --run-id run_gdhs
```

落库：`chip_distribution`（0010）、`holder_stats`（0009）、`daily_bars.turnover`（0008）。

---

### 5.11 基本面深度分析（draft-only）

```bash
# 导出八段底稿（财报全历史+三表+派生指标+一致预期+估值刻度+池内对比）
uv run python -m scripts.pipeline.fundamental_inputs 601318.SH
# → reports/601318.SH/fundamental_inputs_*.json
```

随后由 `fundamental-analysis-skill` 按三模块（事实解读→定性研判→对抗呈现）产出
`reports/{symbol}/fundamental_{date}_draft.md`，人工改写定稿。财务三表/财务摘要手触发：

```bash
uv run python -m scripts.collect.akshare_collect --sources balance_sheet,cash_flow,fin_abstract \
  --date 2026-09-05 --run-id run_fin
```

---

## 6. 核心智能体 / Skill 分工

| 智能体 / Skill | 职责 | 禁止事项 |
|---|---|---|
| `tdx-collect` Skill | 通达信数据源采集；落盘 `data/raw/tdx/` | 不加工、不入库、不评价消息 |
| `stock-collect` Skill | kimi/yahoo 数据源采集；落盘 `data/raw/` | 不加工、不入库、不评价消息 |
| `akshare_collect` Python 脚本 | akshare 数据源采集（每日主源）；落盘 `data/raw/akshare/` | 不加工、不入库、不评价消息 |
| `fred-valuation-card-skill` | 消费系统底稿，生成估值排期卡 Markdown + 卡片 JSON draft | 不直接 `activate/reject` 卡片；不计算指标 |
| `fundamental-analysis-skill` | 消费 fundamental_inputs 底稿，产出基本面分析 draft（三模块） | 不产规范化数字；不做买卖建议；不进日报/信号链 |
| `message-tag-skill` | 消息面事件打标（LLM 通道） | 不产规范化数字；标签须人审生效 |
| `trade-winrate-odds` Skill | 胜率打分辅助（如调用） | 不直接改变仓位或执行记录 |
| `earnings-surge-screener` | 财报异动筛选 | 不替代完整估值排期流程 |
| Python 管线 | 所有规范化数字、指标、信号、报告 | — |
| 人工 | 卡片激活/拒绝、执行确认、模拟盘决策、draft 定稿 | — |

---

## 7. 桌面 Agent 使用指南（opencode / Claude Code / Codex CLI 等）

本项目的日常运维与开发大量由桌面编码智能体完成（本仓库的执行日志即人机协作记录）。
以下是用桌面 Agent 驱动本项目的实践方法。

### 7.1 启动与入口

```bash
cd ~/path/to/trade
opencode        # 或 claude / codex 等，在项目根目录启动
```

- Agent 启动后**自动读取 `AGENTS.md`**（硬性约定精简版：uv 管理、pytest 全绿、
  文档同步、LLM 边界、卡片人工激活等）——无需口头重复。
- 首次会话先让它读交接文档：**「先读 docs/handoff.md」**——内含环境、常用命令、
  约定、当前状态（截至最近一次执行的补记）。
- 深入开发前按 `docs/handoff.md` 开头的阅读顺序走：`system_design.md` →
  `execution_log.md` → `database_schema.md`。

### 7.2 典型任务一句话驱动

| 任务 | 对 Agent 说 |
|---|---|
| 每日盘后例行 | 「跑一下盘后 daily」（自动：八源采集 → daily 管线 → 报告 → 执行日志） |
| 数据疑问排查 | 「查一下 xx 股票 09-05 为什么 degraded，先看报告第 8 段和 signal_facts」 |
| Bug 修复 | 「修复 xxx，先在 execution_log 找类似案例」 |
| 新功能 | 「做一个 xx 的设计」→ Agent 走 brainstorming → 设计文档写入
  `docs/superpowers/specs/` → 交外部 Agent 评审 → 修订 → writing-plans → 分任务实施
  （本项目模拟盘/筹码分布均按此流程演练） |
| 消息面研判 | 粘贴新闻/电报 → 按 message-tag-skill 纪律打标（不产数字、事实推断观点三分） |
| 基本面深查 | 「分析一下 xx 股票」→ fundamental-analysis-skill 产 draft，人工定稿 |

### 7.3 环境注意事项

- **所有命令前缀 `uv run`**；包下载失败走系统代理（macOS/zsh）。
- Agent 每次改动会**追加 `docs/execution_log.md`**、同步设计文档、跑 `pytest -q`
  全绿才算完成——这是 AGENTS.md 的硬性约定，不要让它跳过。
- **提交规范**：小步提交、中文 conventional commits（`feat(paper): ...` /
  `fix(llm): ...` / `docs: ...`），见 `git log`。
- **数据与密钥不入库**：`data/`（数据库/原始 CSV）在 `.gitignore`；API key 走
  环境变量；推送前先 `git grep` 扫敏感串。
- **保留人工决断**：卡片 `activate/reject`、真实执行录入、模拟盘决策、draft 定稿
  都必须由人完成——Agent 可以准备一切，但按钮在人手上。

### 7.4 项目内 Skills（Agent 可调用的专项能力）

`skills/` 目录下的 Skill 是给 Agent 用的专项提示词与流程约束：
`fred-valuation-card-skill`（排期卡 draft）、`fundamental-analysis-skill`（基本面
三模块分析）、`message-tag-skill`（消息打标）、`tdx-collect`/`stock-collect`
（采集）。Agent 会按各 SKILL.md 的铁律执行（draft-only、不产规范化数字等）。

---

## 8. 关键目录结构

```
config/              # 配置：watchlist、信号阈值、指标参数、宏观因子、日历、paper、UI
  watchlist.yaml
  signals.yaml
  indicators.yaml
  macro_factors.yaml
  paper.yaml         # 模拟盘参数（名义/持有期/窗口）
  calendar_cn_*.yaml

data/
  raw/               # 原始 CSV（按来源/类型/日期/运行批次存放）
  market.db          # SQLite 主库（派生，可重建）

scripts/
  adapters/          # 数据源解析适配器
  collect/           # 采集客户端与 akshare 采集器
  indicators/        # 指标公式与计算 + 自算筹码分布（chip_distribution）
  pipeline/          # 主管线：daily、db、adjust、weekly、ingest、report、card、execution、
                     #   card_inputs、fundamental_inputs、pit_backfill、calendar_check
  signals/           # 各类信号：weekly、daily_watch、right_side、accumulation、
                     #   corporate_action、factor_watch
  paper/             # 模拟盘：决策点枚举/录入/结算/统计（与 backtest、executions 隔离）
  backtest/          # AKQuant 回测子包（与管线完全隔离）
  ui/                # Flask 只读 Web UI

skills/
  fred-valuation-card-skill/    # 排期卡 Skill（draft-only）
  fundamental-analysis-skill/   # 基本面深度分析 Skill（draft-only）
  message-tag-skill/            # 消息面打标 Skill
  tdx-collect/                  # 通达信采集 Skill
  stock-collect/                # kimi/yahoo 采集 Skill

cards/{symbol}/      # 排期卡存档（current.md、版本、inputs、draft）
reports/             # 单股报告 + 全池日报 + 基本面 draft/底稿
docs/                # 设计、计划、执行日志、数据库说明、specs/plans
tests/               # pytest 测试集
```

---

## 9. 常用命令速查

```bash
# 每日盘后主入口（先采集后管线）
uv run python -m scripts.collect.akshare_collect --sources price,forward,financials,index,telegraph,announcement,macro,flow --start 2023-08-01 --end 2026-09-05 --date 2026-09-05 --price-api sina --run-id run_daily_20260905
uv run python -m scripts.pipeline.daily --date 2026-09-05 --raw-dir data/raw/akshare

# 导出排期卡底稿
uv run python -m scripts.pipeline.card_inputs 603605.SH

# 卡片管理
uv run python -m scripts.pipeline.card create-draft 603605.SH --json cards/603605.SH/draft_2026-09-05.json
uv run python -m scripts.pipeline.card activate 603605SH_xxxx --effective-from 2026-09-05
uv run python -m scripts.pipeline.card reject 603605SH_xxxx

# 执行记录
uv run python -m scripts.pipeline.execution add --symbol 603605.SH --action buy --price 55.00 --quantity 1000 --fees 5.50 --tier 1 --executed-at 2026-09-05

# 模拟盘
uv run python -m scripts.paper.decide --pending
uv run python -m scripts.paper.decide --pick 25 --decision follow
uv run python -m scripts.paper.settle run
uv run python -m scripts.paper.stats

# 自算筹码分布
uv run python -m scripts.indicators.chip_distribution --all

# 报告
uv run python -m scripts.pipeline.report --date 2026-09-05 --symbol 603605.SH
uv run python -m scripts.pipeline.report --date 2026-09-05

# UI
uv run python -m scripts.ui.app

# 测试
uv run pytest -q
```

---

## 10. 关键设计约束（违反会出错）

- **复权 vs 不复权**：指标/周线用复权；排期卡价区/证伪线/箱体/现价用不复权。两边比较前必须 ÷ 当日因子折回。
- **不猜**：关键数据缺失输出 `incomplete/degraded` 及原因码，禁止伪装成“条件满足”。
- **无未来函数**：信号只使用当时可见数据，均量基数 `shift(1)`，样本不足不判定。
- **LLM 边界**：LLM 只消费系统底稿（card_inputs / fundamental_inputs / 事件清单），产出 draft；`activate/reject` 必须人工。
- **模拟盘隔离**：模拟决策与真实执行（executions）完全隔离；成交价系统取不可自填；决策 append-only。
- **文档同步**：任何改动追加 `docs/execution_log.md`；设计变更同步 `docs/system_design.md`。
- **幂等重跑**：所有派生表采用 DELETE + 重插 + `pipeline_runs` 记录。

---

## 11. 必读文档

开发或维护前，请按以下顺序阅读：

1. `docs/system_design.md` —— 设计基线与契约
2. `docs/implementation_plan.md` —— 阶段计划
3. `docs/execution_log.md` —— 执行历史与偏差决定
4. `docs/handoff.md` —— 其他智能体交接入口
5. `docs/database_schema.md` —— 数据库逐表逐字段说明
6. `AGENTS.md` —— 硬性约定精简版

---

## 12. 故障排查

- **测试失败**：先运行 `uv run pytest -q`，定位后查看 `docs/execution_log.md` 是否已有类似记录。
- **因子重建误报/每日全量重建**：参见 `docs/execution_log.md` 2026-08-10 晚间记录；根因是新 bar 因子占位 1.0 进入重叠窗口。当前 workaround 是使用 3 年 full forward 文件。
- **akshare 采集缺字段或空**：检查 `data/raw/akshare/.../_meta.json` 中的 `errors`。
- **UI 启动失败**：确认 `TRADE_DB_PATH` 或默认 `data/market.db` 存在且可读写。
- **卡片相关信号为 incomplete**：无 active 卡或卡片尚未生效属预期，见 `scripts/pipeline/card.py`。

---

*项目版本：0.2.0 · trade-analysis*
