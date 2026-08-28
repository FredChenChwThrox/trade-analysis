# 消息面研判系统设计（r2 合并稿）

> 状态：设计稿 r2（合并版）
> 取代：`docs/superpowers/specs/2026-08-26-message-eval-design-r1.md`（r1，机器流水线视角）。
> 吸收：`~/Downloads/股票消息面研判系统设计.md`（v1.0 2026-08-28，人的研判工作流视角）。
> 关联文档：`docs/system_design.md`（§2.1 点时语义、§2.5 不猜、§3.6 公告与新闻、§5.5 消息评价与事件研究、§8.1 每日盘后阶段 6）、`docs/database_schema.md`（§6 events / event_symbols / event_assessments）、`AGENTS.md`（LLM 受限参与）。

## 1. 定位与核心思想

消息面研判不是"收集新闻"，而是回答三个问题：**这事改变了什么、市场定价了多少、头寸要不要动**。

两份前身稿的分工在本稿中合并为上下层关系：

- **业务规矩层**（来自新稿）：四层分类、四道筛子、结构化标签、证伪条件、判断归因——定义"怎么研判"。
- **机器实现层**（来自 r1）：采集、幂等落库、确定性关联、LLM 初判、人审 gate、报告渲染——定义"怎么自动跑"。

**铁律（两份前身稿共识，本稿继承）**：

- 消息面不允许单独触发计划外操作；所有动作必须落回三档排期卡和波段规则，由人工执行。
- 不预测涨跌、不输出买卖建议；LLM 不产数字（§5.5），价格/收益数字只由确定性 `event_study.py` 产生。
- 原始数据一律先落库再处理；任一环节失败降级不阻断主流程（§2.5 / §8.1）。

**人机分工**：

```
L4 决策层   人：读原文、定动作、写证伪条件          ← judgments 表
L3 研判层   LLM 初判 + 人确认：结构化标签、预期差   ← event_assessments + 人审 UI
L2 分发层   机器：去重、信源分级、打标、路由        ← 确定性代码
L1 采集层   机器：定时脚本（akshare）              ← 现有采集体系扩展
L0 日历层   人一次性维护 + 系统派生：已知时点事件表 ← event_calendar
```

## 2. 消息分层框架（四层）

`events.scope` 取值扩展为五档（r1 的四档 + 资金/情绪层）：

| scope | 内容 | 作用对象 | 处理策略 |
|---|---|---|---|
| macro | 货币政策、财政刺激、汇率、海外流动性 | 估值中枢 | 周度复核队列，不打扰盘中 |
| policy | 行业监管、补贴、集采、税改 | 估值体系切换 | 周度复核队列；提示是否重画估值锚 |
| industry | 行业景气数据、协会数据 | 行业 EPS 预期 | 关联到 industry_code 命中的 watchlist 股票 |
| company | 财报、业绩预告、增减持回购、激励、诉讼、分红 | EPS 底稿 | **唯一允许即时提醒的层级**；标记"需读原文"；回到底稿重算，可能触发排期档 |
| flow | 龙虎榜、两融、大宗、解禁、指数调样、舆情热度 | 短期供需 | 静默入库，不推送不进日报；做波段确认时才调出 |

### 2.1 信源质量分级（新增）

`events` 加 `source_tier` 列：

| tier | 来源 | 用途 |
|---|---|---|
| 1 | 公告/交易所文件（cninfo、交易所） | 决策链，必须可读原文 |
| 2 | 官媒/部委原文 | 决策链 |
| 3 | 券商研报 | 决策链（价值在高频数据，不在结论） |
| 4 | 财经媒体（财联社电报等） | 决策链入口 + 情绪参考 |
| 5 | 自媒体/股吧/热度榜 | **只做情绪温度计，不进决策链、不上报告消息面段** |

渠道原则：**决策信息看原文，情绪信息看聚合**。

### 2.2 每条消息过四道筛子（研判规矩，落到标签字段）

1. **信源质量**：见 `source_tier`。
2. **增量还是存量（预期差）**：与一致预期的偏离方向与幅度。"符合预期的好消息"常是出货窗口。机器初判可空，**人补**（iFinD/研报一致预期为参照源）。
3. **作用对象**：改 EPS（业绩/订单/成本）还是改 PE（风险偏好/叙事/流动性）。前者回到底稿重算，后者只影响估值区间落点。落 `target` 列。
4. **时效半衰期**：政策拐点季度级、业绩超预告月度级、舆情日级。半衰期决定这条消息有资格驱动哪一档仓位。落 `half_life` 列。

## 3. 数据模型变更（分阶段迁移）

r1 的 migration 0003 未实施，本稿按落地顺序拆为 4 个迁移（§13）。

### 3.1 Phase 1：migration 0003（日历 + 信源分级 + watchlist 扩展）

**新表 `event_calendar` [事实/配置混合]**：

```sql
CREATE TABLE event_calendar (
    cal_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,        -- report_disclosure / unlock / macro_release / fomc / card_review
    symbol          TEXT,                 -- 宏观类为 NULL
    scheduled_date  TEXT NOT NULL,        -- 市场本地日期
    source          TEXT NOT NULL,        -- 'akshare' / 'manual' / 'derived'
    remind_before_days INTEGER NOT NULL DEFAULT 3,
    note            TEXT,
    raw_object_id   TEXT,
    ingested_at     TEXT NOT NULL
);
CREATE INDEX idx_event_calendar_date ON event_calendar(scheduled_date);
```

- 财报披露预约、解禁日程：akshare 批量拉取后人工核对入库（`source='akshare'`）。
- 宏观发布（CPI/社融/议息）：手工维护（`source='manual'`，种子 `config/event_calendar.yaml`）。
- 排期卡 `next_review`：不重复落表，日历视图查询时 union `schedule_cards`（`kind='card_review'` 为派生项）。
- 到期前 `remind_before_days` 天出现在报告"日历提醒"段：提示"检查对应头寸是否落在计划档位内"。

**既有表变更**：

```sql
ALTER TABLE events ADD COLUMN scope TEXT;         -- macro/policy/industry/company/flow
ALTER TABLE events ADD COLUMN source_tier INTEGER; -- 1~5，采集器按来源映射表填充
ALTER TABLE watchlist ADD COLUMN industry_code TEXT;
ALTER TABLE watchlist ADD COLUMN themes_json TEXT;  -- JSON 数组，如 '["油价","航油","汇率"]'
```

- `config/watchlist.yaml` 同步增加 `industry_code` / `themes` 字段；`seed_watchlist` 的 `ON CONFLICT` 列覆盖同步更新（防 seed 清空）。
- `scope` 是事件维度分类（与 event_type 同级的事实派生字段），不是评价列，允许写入 events（r1 §4.4.3 解释保留）。tier 5 来源一期不接入采集，仅预留分级语义。

### 3.2 Phase 2：migration 0004（宏观因子 + 资金/情绪）

**新表 `macro_factors` [事实]**（沿用 r1 §4.1，不变）：

```sql
CREATE TABLE macro_factors (
    factor_type  TEXT NOT NULL,        -- commodity / fx / index_proxy
    code         TEXT NOT NULL,        -- 'BZ=F' / 'AU0' / 'USDCNY=X'
    name         TEXT NOT NULL,
    market       TEXT NOT NULL,        -- CN / GLOBAL
    trade_date   TEXT NOT NULL,
    close        TEXT NOT NULL,        -- 定点 TEXT
    change_pct   TEXT,                 -- 来源原始值；来源无则 NULL，adapter 不计算
    unit         TEXT,
    source       TEXT NOT NULL,
    raw_object_id TEXT,
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (factor_type, code, trade_date)
);
CREATE INDEX idx_macro_factors_recent ON macro_factors(code, trade_date DESC);
```

因子清单固化在 `config/macro_factors.yaml`（商品：Brent/WTI/上海金/上海铜/铁矿/螺纹/白糖/SC 原油；外汇：USD/CNY、USD/HKD、EUR/CNY）。

**资金/情绪事件**：不建新表，统一入 `events`（`scope='flow'`、`source_tier` 按源映射），`event_symbols` 正常关联。采集源：akshare `stock_lhb_*` / `stock_margin_*` / `stock_dzjy_*` / `stock_restricted_release_*` / `stock_hot_rank_em`。**纪律：flow 层事件静默入库，不进日报消息面段、不触发任何推送**；仅在报告"波段确认"场景（二期）或 UI 显式查询时调出。

### 3.3 Phase 3：migration 0005（LLM 评价链）

**新表 `symbol_industry` [事实]**（沿用 r1 §4.2，不变）：

```sql
CREATE TABLE symbol_industry (
    symbol          TEXT NOT NULL,
    industry_code   TEXT NOT NULL,    -- 东财板块码 BKxxxx
    industry_name   TEXT NOT NULL,
    source          TEXT NOT NULL,    -- 'akshare_em'
    classification_date TEXT NOT NULL,
    raw_object_id   TEXT,
    ingested_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, source, classification_date)
);
CREATE INDEX idx_symbol_industry_lookup ON symbol_industry(symbol, source);
```

采集策略：一次性全量 + 季度刷新；不进入 daily stage 6，独立手触发/月度任务。

**重建 `event_assessments` [决策]**（修复 0002 遗留 `assessment_version INTEGER` 亲和问题 + 扩展研判字段）：

```sql
CREATE TABLE event_assessments_new (
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    symbol              TEXT NOT NULL,          -- '__event__' 为事件级行
    assessment_version  TEXT NOT NULL,          -- 'event_study_v1' / 'llm_v1' / ...
    model               TEXT,
    prompt_version      TEXT,
    assessed_at         TEXT NOT NULL,
    event_type          TEXT,
    direction           TEXT,                   -- positive / negative / neutral
    materiality         TEXT,                   -- low / medium / high / critical
    confidence          REAL,
    rationale           TEXT,                   -- ≤300 字，禁预测语言
    target              TEXT,                   -- eps / pe / sentiment（四道筛子第 3 条）
    half_life           TEXT,                   -- day / week / month / quarter（第 4 条）
    expectation_gap     TEXT,                   -- 预期差描述（第 2 条；LLM 初判可空，人补）
    action_hint         TEXT,                   -- none / swing / schedule / redraw_anchor（仅提示，不触发）
    falsification       TEXT,                   -- 证伪条件（人写，LLM 可给建议稿）
    narrative           TEXT,                   -- 逐股叙事（仅 symbol 行，≤150 字）
    status              TEXT NOT NULL,          -- ok / needs_review / degraded
    event_study_json    TEXT,
    run_id              TEXT,
    PRIMARY KEY (event_id, symbol, assessment_version)
);

INSERT INTO event_assessments_new
  SELECT event_id, symbol, assessment_version, model, prompt_version, assessed_at,
         event_type, direction, materiality, confidence, rationale,
         NULL, NULL, NULL, NULL, NULL, NULL,
         status, event_study_json, run_id
  FROM event_assessments;

DROP TABLE event_assessments;
ALTER TABLE event_assessments_new RENAME TO event_assessments;
```

**新表 `event_human_review` [决策]**（沿用 r1 §4.3，不变）：人审动作 confirm / dismiss / upgrade_materiality / note，主键含 `reviewed_at` 支持多次操作；不改写 `event_assessments` 原始 LLM 行。effective_status 解析规则同 r1：未撤销 dismiss → 排除；upgrade_materiality → 覆盖显示；confirm → ok；否则取 `event_assessments.status`。

人审 UI 在人审时**可就地补写** `expectation_gap` / `falsification` / 修正 `target` / `half_life`（人写覆盖 LLM 初判，写 `event_human_review` action='amend'，payload 存新值；报告渲染 join 优先取人审值，原始 LLM 行仍不动）。

### 3.4 Phase 4：migration 0006（判断闭环）

**新表 `message_judgments` [决策]**——L4 决策层产物，判断可复现、可审计：

```sql
CREATE TABLE message_judgments (
    judgment_id     TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES events(event_id),
    symbol          TEXT,
    judgment        TEXT NOT NULL,        -- 判断陈述
    action_taken    TEXT NOT NULL,        -- none / swing / schedule / redraw_anchor
    falsification   TEXT NOT NULL,        -- 证伪条件：什么后续数据出现则此判断作废
    review_due      TEXT NOT NULL,        -- 复核日期（按 half_life 定）
    outcome         TEXT NOT NULL DEFAULT 'pending',  -- pending / correct / wrong / expired
    attribution     TEXT,                 -- 错判归因：source / expectation_gap / half_life / other
    actor           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT,
    payload_json    TEXT
);
CREATE INDEX idx_judgments_due ON message_judgments(review_due) WHERE outcome='pending';
```

- 到期未复核的判断在报告"日历提醒"段弹出；复核时对则留档（`correct`），错则归因（`wrong` + attribution）。
- 该表与 `event_human_review` 的分工：后者是"对机器标签的确认/否决"，前者是"人的决策与事后归因"，积累成可回测样本库。

## 4. 采集层（L1）

| 源 | scope/tier | 方式 | 阶段 |
|---|---|---|---|
| cninfo 公告 | company / 1 | 已有 `adapters/announcements.py`，接入 daily stage 6 增量 | Phase 1 |
| 财报披露预约、解禁日程 | 日历 | akshare 批量 + 人工核对 → event_calendar | Phase 1 |
| 财联社电报 | 按内容定 / 4 | 现有 `parse_telegraph_csv` + 采集 CLI `--sources telegraph` | Phase 2 |
| 商品/外汇快照 | macro 背景 | 新 `parse_macro_csv` → macro_factors | Phase 2 |
| 龙虎榜/两融/大宗/解禁/热度榜 | flow / 3~5 | 新 adapter，静默入库 | Phase 2 |
| 行业分类 | — | `parse_industry_csv` → symbol_industry | Phase 3 |
| 部委政策原文、协会数据（RSS/官网） | policy / 2 | 二期 | 二期 |

工程纪律（沿用现有体系）：raw 落盘 + content_hash + run 记录；pin 版本；单源失败记日志不中断流水线；新源首采后必须核对 CSV 实际落盘（2026-08-28 踩坑教训）。

## 5. 分发层（L2，确定性）

`scripts/signals/event_link.py` 扩展为路由+关联一体：

1. **信源分级**：采集器按来源映射表写 `source_tier`。
2. **层级分类**：关键词规则初分 scope（"降准/MLF"→macro；部委名→policy；公告类型字段→company；龙虎榜等源天然 flow），LLM 评价时复核修正。
3. **关联候选**（沿用 r1 §7，优先级去重）：
   - ① event_symbols 已含的 watchlist symbol（公告/电报文本匹配别名/六位码）；
   - ② scope ∈ (industry, policy, macro) 且 industry_code JOIN 命中；
   - ③ themes_json 词边界匹配（仅 scope ∈ industry/policy/macro；避免"黄金"命中"黄金周"）。
   写 `event_symbols` 用 `INSERT OR IGNORE`，不破坏手工关联。
4. **路由规则**：
   - macro / policy → 周度复核队列，不打扰盘中；
   - **company（tier ≤ 2）→ 报告置顶 + UI 标记"需读原文"**（一期盘后批量；即时推送为二期）；
   - flow → 静默，不推送不进日报。

**提醒纪律：只有公司层公告允许提醒。** 提醒泛滥的系统两周就会被关掉。

## 6. 研判层（L3，LLM 初判 + 人确认）

### 6.1 执行顺序（沿用 r1 §6.1，解决循环依赖）

```
daily stage 6a:  采集（公告 + 电报 + macro 可选 + flow 静默）
daily stage 6b1: 事件级 LLM 初判
  → scope 复核 / direction / materiality / confidence / target / half_life / rationale
  → 写 events.scope + event_assessments(event_id, '__event__', 'llm_v1')
daily stage 6c:  关联层（§5，确定性）→ event_symbols
daily stage 6b2: 逐股叙事 LLM → event_assessments(event_id, symbol, 'llm_v1')
```

### 6.2 LLM 输出 schema（写库前校验）

```json
// 事件级（新增 target / half_life / expectation_gap / action_hint / falsification_suggestion）
{
  "scope": "macro | policy | industry | company | flow",
  "direction": "positive | negative | neutral",
  "materiality": "low | medium | high | critical",
  "confidence": 0.0,
  "target": "eps | pe | sentiment | null",
  "half_life": "day | week | month | quarter | null",
  "expectation_gap": "string | null（LLM 无一致预期数据，默认 null，人补）",
  "action_hint": "none | swing | schedule | redraw_anchor",
  "falsification_suggestion": "string | null（建议稿，人审定稿）",
  "rationale": "string, ≤300 字, 禁用预测/建议语言"
}
// 逐股叙事
{ "narrative": "string | null, ≤150 字, 禁用预测/建议语言" }
```

- 提示词明示"不得输出数字、不得预测价格、不得建议买卖"；输出严格 JSON，解析失败丢弃（不冒充）。
- `action_hint` 仅为提示，**不构成触发**；动作落回排期卡/波段规则，人工执行。
- 调用策略沿用 r1 §6.3：批量 20、并发上限 8、单事件超时 30s、指数退避、`max_llm_calls_per_run: 1200`、配置走 `config/llm.yaml`。

### 6.3 混合人审 gate（沿用 r1 §3.5）

```
IF materiality IN ('high','critical')
   OR confidence < 0.4
   OR rationale 命中禁用敏感词表
   OR (scope='company' AND source_tier <= 2)   -- 新增：公司层重大公告一律人审
THEN status='needs_review' ELSE status='ok'
```

- `needs_review` 不进报告消息面段，进 Web UI 人审列表（每日盘后 10 分钟例行人审，不只是被动处理）。
- 人审时确认/修改机器标签，补 `expectation_gap`、定稿 `falsification`；公司层消息点进巨潮 PDF 读原文（成色词"拟/原则上/研究"要人看）。

## 7. 决策层（L4，规则闸门）

```
这条消息 → 改 EPS 底稿？ → 是：重算排期卡锚点（人工发起 card_inputs 复核）
         → 只改 PE/情绪？ → 查股价位置 + 衰竭信号打分（报告已展示，人工判读）
         → 动作 ∈ {不动, 波段仓加减, 档位触发, 重画锚} —— 全部人工执行
         → 写 message_judgments：判断 + 证伪条件 + 复核日期（按 half_life）
```

到期判断自动出现在报告"日历提醒"段：对则留档，错则归因（信源问题 / 预期差判错 / 半衰期判错）。

## 8. 报告层

### 8.1 单股报告新增段

插入位置：现有 §6.2 单股报告"观察点"与"衰竭信号"之间。

```
## 4.x 日历与消息面

### 日历提醒（未来 3 日）
- [2026-08-31] 排期卡复核到期（12 张集中到期，逐张 card_inputs 对照）
- [2026-09-02] 财报披露预约：xxx——检查头寸是否落在计划档位内

### 公司公告（tier ≤ 2，置顶，标"需读原文"）
- [company] 2026 中报：营收/归母…… direction=neutral materiality=high
  ※ 需读原文：成色词与分部数据以巨潮 PDF 为准

### 消息面（LLM 初判 + 人审后）
### 行业层 / 宏观政策层 / 直接命中（同 r1 §8.1 三层聚合）
- [industry] [OPEC+ 延长减产] direction=positive materiality=high confidence=0.82
  target=pe half_life=quarter 预期差：（人补）
  叙事：……
  事件研究：base 2026-08-22 收 8.12，T+1 +0.6%（超额 +0.5%），T+5 pending
  价格位置：现价距 T2 档上沿 +1.8%，衰竭信号 0 项   ← 确定性 join，见 §8.2
```

**触发条件**（沿用 r1）：effective_status='ok' 且 narrative 非空且 `available_at ≤ as_of`。

### 8.2 消息 × 价格位置交叉验证（新增，确定性）

- 报告每条事件旁附该股**价格位置**一行：距最近排期档距离、当前衰竭信号数——由确定性代码 join 现有信号表，LLM 不参与。
- **背离样本**：`event_study` 结果落地后，direction 与 T+1/T+5 实际方向相反（利好不涨/利空不跌）时在 `event_assessments.event_study_json` 加 `divergence: true` 标记——定价里已有未知信息，是最值得记录的样本，人审时优先复核。

### 8.3 报告快照扩展

`report_runs.input_snapshot_json` 加 `message_eval` 子结构（沿用 r1 §8.3）+ `calendar_due` 计数 + `judgments_due` 计数。

### 8.4 flow 层不上日报

资金/情绪事件仅静默入库；二期在波段确认场景调出。

## 9. 失败与降级（沿用 r1 §9，补充日历行）

| 失败点 | 表现 | 报告展示 |
|---|---|---|
| 公告/电报采集空 | events 当日无新增 | "消息面：今日无新增事件" |
| 日历源缺失 | event_calendar 无到期项 | 日历提醒段省略 |
| 商品/外汇采集失败 | macro_factors 当日缺 | 底稿缺因子，提示词"忽略缺失" |
| LLM 6b1 失败 | status='degraded'，events.scope 不更新 | 事件不出现 |
| LLM 6b2 失败 | narrative NULL | 该事件在该股维度不出现（不冒充） |
| 关联层失败 | 部分 event_symbols 未生成 | 仅展示已成功关联的 |
| symbol_industry 缺失 | JOIN 无结果 | 仅展示直接命中部分 |

整体 stage 6 失败在 `pipeline_runs` 写 degraded，不阻断报告生成。

## 10. Web UI

- `/message-review` 人审页（沿用 r1 §10）：confirm / dismiss / upgrade_materiality / note + **amend**（补预期差、定稿证伪条件、修正 target/half_life）；详情展开底稿 JSON、LLM 输出、event_study_json、价格位置。
- `/message-review` 增加 judgments 录入入口（L4）：判断、动作、证伪条件、复核日期。
- 日历提醒并入现有 `/cards` 页顶部横幅（到期卡复核 + event_calendar 到期项）。
- API：`POST /api/event-review/{confirm,dismiss,upgrade,note,amend}`、`POST /api/judgments/{create,review}`，仅写 `event_human_review` / `message_judgments` 两表。

## 11. 配置

- `config/llm.yaml`（沿用 r1 §11）：模型、批量、超时、成本护栏、`review_gate`（新增 company+tier≤2 强制人审规则）。
- `config/macro_factors.yaml`（沿用 r1 §5.2）。
- `config/event_calendar.yaml`（新）：手工宏观/议息日历种子。
- `config/watchlist.yaml`：补 `industry_code` / `themes`。
- ⚠️ 首版参数人工核对期纪律沿用 §5.2。

## 12. 测试策略

- 单元：`test_event_link.py`（关联+路由+词边界）、`test_macro_factors.py`、`test_event_eval_schema.py`（扩展字段校验）、`test_event_assessment_llm.py`（gate 含 company 规则、幂等）、`test_event_human_review.py`（effective_status 含 amend）、`test_event_calendar.py`（到期提醒窗口、派生 union）、`test_message_judgments.py`（到期弹出、归因流转）、`test_divergence.py`（背离标记）。
- 集成：`test_message_eval_pipeline.py`（fixtures → ingest → 6b1 模拟 → 6c → 6b2 → 落库 → 报告渲染，断言仅展示命中事件 + 日历段 + 价格位置行）。
- Golden：不新增（LLM 输出非确定性）。
- 人工核对期：LLM rationale 每周抽样 20 条；judgments 归因每月复盘一次。

## 13. 落地顺序（渐进，对齐新稿"第一周 80% 价值"）

1. **Phase 1（日历 + 公告，无 LLM）**：migration 0003；`config/event_calendar.yaml` 种子；cninfo 公告接入 daily stage 6 采集；报告"日历提醒 + 公司公告"段；`/cards` 日历横幅。→ 先消灭"意外"，覆盖大部分日常价值。
2. **Phase 2（宏观背景 + 资金情绪静默入库）**：migration 0004；telegraph 持续采集 CLI；macro_factors 采集；flow 层采集入库（不推送）。
3. **Phase 3（LLM 评价链）**：migration 0005；`scripts/llm/` 模块 + 提示词 + schema 校验；6b1/6c/6b2 编排；`/message-review` 人审 UI；报告消息面段完整渲染 + 价格位置行。
4. **Phase 4（判断闭环）**：migration 0006；judgments 录入/复核 UI；背离样本标记；样本库积累后，再把"信源质量 × 预期差 × 半衰期"做成加权打分（二期）。

每 Phase 完成即追加 `docs/execution_log.md`，设计变更同步 `docs/system_design.md` 与 `docs/database_schema.md`。

## 14. 验收标准

- 任一阶段失败不阻断报告，`pipeline_runs` 写 degraded。
- 报告仅展示 effective_status='ok' + 命中该股的事件；company+tier≤2 未经人审不出现。
- LLM 不产数字；rationale 无预测语言；数字只来自 event_study。
- 幂等：相同 raw / 相同输入重跑不重复写。
- 关联层 INSERT OR IGNORE，不破坏手工关联。
- flow 层事件不出现在日报。
- 日历提醒覆盖：排期卡 next_review + event_calendar 到期项。
- judgments 到期弹出复核，可归因闭环。
- `assessment_version` 统一 TEXT，兼容 event_study_v1 / llm_v1。
- 测试全绿。

## 15. 二期预留

- 公司层公告盘中即时推送（需盘中采集轮询）。
- flow 层数据接入波段确认场景；部委 RSS/官网监控；iFinD 人工研判线对接（预期差、行业景气、财报拆解的参照源，走对话查询不进 cron）。
- 主题关联升级到 LLM（draft-only，§3.6 纪律）。
- judgments 样本库回测与加权打分。
- 海外 PMI/利率接入 macro_factors；events.scope 多版本历史。
