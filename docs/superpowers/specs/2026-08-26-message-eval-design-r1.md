# 消息面/宏观对监控列表股价影响分析（D3 消息评价）

> 状态：设计稿 r1（已 review 并调整）
> 关联文档：`docs/system_design.md`（§3.6 公告与新闻、§5.5 消息评价与事件研究、§8.1 每日盘后阶段 6、§9.4 上线顺序 D3）、`docs/database_schema.md`（§6 events / event_assessments）、`AGENTS.md`（LLM 受限参与）。
> 取代：`docs/superpowers/specs/2026-08-26-message-eval-design.md`（首版）。

## 1. 目标

把现有 daily 流水线 stage 6（§8.1；当前只跑确定性 `event_study`）扩成完整消息评价链路：

1. 财联社电报持续采集入 `events`（已有 akshare adapter）。
2. 新增 akshare 商品/外汇快照入 `macro_factors`，作为宏观因子背景。
3. LLM 对每条事件产出语义评价（direction / materiality / confidence / scope / rationale），结果落 `event_assessments`。
4. 对 watchlist 中每只股票（含通过 `industry_code` 间接关联），由 Python 派生"今日与该股相关的事件集合"，并由 LLM 生成逐股叙事。
5. 单股报告新增"消息面"段，**仅展示 status='ok' 且与该股今日关联的事件**。
6. Web UI 新增人审列表页，运营可对 `status='needs_review'` 的事件一键 `dismiss` 或升级 `materiality`（不改变 LLM 原始产出，仅写审计字段）。

**非目标**：
- 不预测涨跌、不输出买卖建议（§1.2）。
- 不做盘中实时滚动评价（与选项 B 决定一致：盘后批量 + 人审 UI）。
- 不替排期卡触发逻辑（消息面仅是观察段）。
- 不引入 LLM 评价 draft-only 激活机制（一期 status 直生效，但需混合人审 gate：见 §3.5）。

## 2. 总体架构

```
采集层（akshare）
  ├─ 财联社电报       → events / event_symbols              (已有 adapter，扩展)
  ├─ 商品快照         → macro_factors                       (新 adapter)
  └─ 行业分类         → symbol_industry                     (新 adapter，一次性 + 季度刷新)

评价层（LLM，stage 6b）
  ├─ 6b1 事件级评价   → event_assessments (__event__ 行)
  │                     (event_id, symbol='__event__', 'llm_v1')
  └─ 6b2 逐股叙事     → event_assessments (symbol 行)
                        (event_id, symbol, 'llm_v1')

关联层（Python，stage 6c，确定性）
  ├─ 直接命中：event_symbols 已含的 watchlist 股票
  ├─ 行业命中：symbol_industry JOIN watchlist.industry_code
  │            把 scope in ('industry','policy','macro') 的事件
  │            关联到 industry_code 命中的 watchlist 股票
  │            （写入 event_symbols；idempotent 冲突跳过）
  └─ 主题命中：watchlist.themes_json 子串/词边界匹配
               命中 scope in ('industry','policy','macro') 的标题/摘要
               仅对 themes 命中且该 symbol 无直接/行业关联时补充
               （写入 event_symbols；idempotent 冲突跳过）

报告层（§6.2 单股报告 §6.2.4 段）
  └─ "消息面" 子节：列 status='ok' 且通过关联层命中该股的当日事件
                    展示：标题、scope×direction 双标签、materiality、confidence、rationale、
                          event_study_json 的 T+1/T+5（若存在）。
```

### 2.1 三层（宏观 → 行业 → 个股）的实现

**不在库内为这三层单独建表**——三层均为聚合视图，源数据是 per-event 与 per-symbol 评价：
- **宏观层** = 报告"今日宏观面"开头段，对 `scope in ('macro','policy')` 的事件按 `direction × materiality` 聚合并嵌入主索引股票池共性观察。
- **行业层** = 单股报告"消息面"段开头部分，对该股所在 industry_code 上 `scope='industry'` 的事件聚合。
- **个股层** = "消息面"段逐条事件：含事件级评价 + 逐股叙事 + T+1/T+5。

## 3. 边界与纪律（与既有契约对齐）

- **§2.1 点时语义**：`available_at` 严格沿用 `events.available_at`（电报即时发布 = `published_at`）。逐股叙事只在 `available_at <= as_of` 时计算。
- **§2.5 不猜**：LLM 评价失败 → stage 6 整体 degraded；逐股叙事缺失 → 该事件在该股维度上不出现（不冒充）。
- **§5.5 LLM 不产数字**：评价仅含方向/重要性/置信/范围/理由。所有价格/收益数字由确定性 `event_study.py` 写 `event_study_json`（既有）。
- **§3.6 draft-only 纪律例外**：本功能一期不设 LLM draft 激活（事件不经过人工确认就落 `event_assessments`），但设置 **混合人审 gate**（§3.5）：
  - `materiality='high'/'critical'` 的事件 → 默认 `status='needs_review'`；
  - `confidence < 0.4` 或 rationale 命中敏感关键词 → 默认 `status='needs_review'`；
  - 其余事件 → `status='ok'`，可直接入单股报告；
  - 报告渲染时只取 `status='ok'`；
  - 人审 UI 中运营可将 `needs_review` 一键 `confirm` 变为 `ok`，或 `dismiss` 排除。
- **§6.4 语言纪律**：评价 rationale 不允许出现"看涨/看跌/建议买入"等预测语言；提示词模板强制约束。
- **§8.3 幂等**：
  - events 入库沿用现有 source_external_id + content_hash 去重。
  - macro_factors 主键 `(factor_type, code, trade_date)`，相同值跳过。
  - event_assessments 落库前检查 `(event_id, symbol, assessment_version)` 是否已存在；存在且 status='ok' 或 'needs_review' 则跳过；'degraded' 行可重算覆盖。
  - industry → watchlist 关联层写 `event_symbols` 用 `INSERT OR IGNORE`，不破坏现有手工关联。
- **§9.5 硬门槛**：raw 落盘 + content_hash + run 记录；JSON 列写库前校验 schema；金额/价格/汇率用定点。
- **§9.5 软约束**：本设计不改变既有的单字段 Decimal 降级做法。

### 3.5 混合人审 gate（一期新增）

```text
LLM 输出事件级评价后：
  IF materiality IN ('high', 'critical')
     OR confidence < 0.4
     OR rationale 命中禁用敏感词表（看涨/看跌/建议买入/建议卖出/目标价/等）
  THEN status = 'needs_review'
  ELSE status = 'ok'
```

- `needs_review` 行在单股报告中不出现，但进入 Web UI 人审列表。
- 运营可在 UI 中 `confirm`（写 `event_human_review` action='confirm'，报告随后取该 symbol 最新人审状态）或 `dismiss`（从该股今日相关列表排除）。
- `confirm` 不改变 `event_assessments` 的原始 LLM 行，只在报告渲染时 join `event_human_review` 取 `effective_status`。
- 该 gate 与排期卡"draft-only/人工确认激活"的纪律一致：LLM 产物需经门槛过滤后方可影响报告输出。

## 4. 数据模型变更

### 4.1 新表：`macro_factors` [事实]

```sql
CREATE TABLE macro_factors (
    factor_type  TEXT NOT NULL,        -- commodity / fx / index_proxy
    code         TEXT NOT NULL,        -- e.g. 'BZ=F' (Brent), 'AU0' (gold SHFE), 'CNYHKD=X'
    name         TEXT NOT NULL,        -- 人读名
    market       TEXT NOT NULL,        -- CN / GLOBAL
    trade_date   TEXT NOT NULL,        -- 市场本地日期
    close        TEXT NOT NULL,        -- 定点 TEXT
    change_pct   TEXT,                 -- 定点 TEXT（来源原始值；若来源无则 NULL，不在 adapter 计算）
    unit         TEXT,                 -- 'USD/bbl' / 'CNY' / 'CNY per USD' 等
    source       TEXT NOT NULL,        -- 'akshare'
    raw_object_id TEXT,                -- raw_objects 外键语义
    ingested_at  TEXT NOT NULL,        -- UTC
    PRIMARY KEY (factor_type, code, trade_date)
);
CREATE INDEX idx_macro_factors_recent ON macro_factors(code, trade_date DESC);
```

**因子清单**（一期固化，`config/macro_factors.yaml` 种子）：
- 商品：Brent `BZ=F`、WTI `CL=F`、上海金 `AU0`、上海铜 `CU0`、铁矿 `I0`、螺纹钢 `RB0`、白糖 `SR0`、原油 SC `SC0`（视可用源调整）。
- 外汇：USD/CNY `USDCNY=X`、USD/HKD `USDHKD=X`、EUR/CNY `EURCNY=X`。
- 不接海外利率/PMI 一期（无源）；二期可补。

**change_pct 纪律**：`change_pct` 必须是 akshare 接口直接返回的原始字段；adapter 不计算。若接口不提供，则存 NULL，LLM 提示词会说明"忽略缺失"。

### 4.2 新表：`symbol_industry` [事实]

```sql
CREATE TABLE symbol_industry (
    symbol          TEXT NOT NULL,
    industry_code   TEXT NOT NULL,    -- 东财行业码，如 'BK0438'
    industry_name   TEXT NOT NULL,    -- 人读名，如 '航空机场'
    source          TEXT NOT NULL,    -- 'akshare_em'
    classification_date TEXT NOT NULL,    -- 源数据日期（行业分类可能漂移）
    raw_object_id   TEXT,
    ingested_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, source, classification_date)
);
CREATE INDEX idx_symbol_industry_lookup ON symbol_industry(symbol, source);
```

**采集策略**：一次性全量同步 + 季度定时刷新；akshare `stock_board_industry_name_em` + `stock_industry_clf_em` 组合获取每只股票的行业码。**行业码固定采用东财板块码 `BKxxxx`**；`source='akshare_em'`。同一 symbol 多源并存时 `eastmoney` 优先；同 symbol 多个行业码时全存（罕见），报告/关联时取最新 `classification_date` 且 `source='akshare_em'`。

### 4.3 新表：`event_human_review` [决策]

人审状态与审计字段；不修改 `event_assessments`，避免覆盖 LLM 原始产出。

```sql
CREATE TABLE event_human_review (
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    symbol              TEXT NOT NULL,                -- '__event__' or 股票 symbol
    assessment_version  TEXT NOT NULL,                -- 'llm_v1'
    action              TEXT NOT NULL,                -- 'confirm' / 'dismiss' / 'upgrade_materiality' / 'note'
    payload_json        TEXT NOT NULL,                -- JSON：变更详情
    actor               TEXT NOT NULL,
    reviewed_at         TEXT NOT NULL,                -- UTC
    PRIMARY KEY (event_id, symbol, assessment_version, action, reviewed_at)
);
```

主键追加 `reviewed_at` 以支持同一事件多次操作（如先 confirm 再升级）。

- `confirm`：对 `needs_review` 事件，运营确认后报告取 `effective_status='ok'`。
- `dismiss`：对任意 `event_id, symbol` 行，从该股"今日相关"列表排除（仅对当前评估版本生效；LLM 重跑后失效）。
- `upgrade_materiality`：写入"high/critical"覆盖显示；不写回 `event_assessments`，报告渲染时 join 优先取人审结果。
- `note`：仅备注，不改变生效状态。

**生效状态解析**（报告/UI 统一函数）：
```textneffective_status(event_id, symbol, version):
  若存在未撤销的 dismiss → 排除
  若存在 upgrade_materiality → 使用升级后的 materiality，其余字段仍取 event_assessments
  若存在 confirm → status='ok'
  否则 → event_assessments.status
```

### 4.4 既有表变更

#### 4.4.1 `watchlist` 加列 [配置]

```sql
ALTER TABLE watchlist ADD COLUMN industry_code TEXT;
ALTER TABLE watchlist ADD COLUMN themes_json TEXT;     -- JSON 数组
```

- `industry_code`：从 `symbol_industry` 反查覆盖；不在 `symbol_industry` 的股票 `industry_code` 为 NULL（仅靠 `event_symbols` 直接命中）。
- `themes_json`：扩展事件匹配，例如 `["油价","航油","汇率"]`。
- **YAML 同步**：`config/watchlist.yaml` 增加 `industry_code` 和 `themes` 字段；`scripts/pipeline/db.py seed_watchlist` 更新 `ON CONFLICT` 列覆盖，避免 seed 后数据库字段被清空。

#### 4.4.2 `event_assessments` 重建 [决策]

修复 `0002` 迁移遗留的 `assessment_version INTEGER` 类型问题，兼容现有 `event_study_v1` TEXT 值。

```sql
CREATE TABLE event_assessments_new (
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    symbol              TEXT NOT NULL,
    assessment_version  TEXT NOT NULL,    -- 'event_study_v1' / 'llm_v1' / ...
    model               TEXT,
    prompt_version      TEXT,
    assessed_at         TEXT NOT NULL,    -- UTC
    event_type          TEXT,
    direction           TEXT,             -- positive / negative / neutral
    materiality         TEXT,             -- low / medium / high / critical
    confidence          REAL,             -- 0.0~1.0
    rationale           TEXT,
    status              TEXT NOT NULL,    -- ok / needs_review / degraded
    event_study_json    TEXT,
    run_id              TEXT,
    PRIMARY KEY (event_id, symbol, assessment_version)
);

INSERT INTO event_assessments_new SELECT * FROM event_assessments;

DROP TABLE event_assessments;
ALTER TABLE event_assessments_new RENAME TO event_assessments;
```

- `scope` 不加入 `event_assessments`（避免每 symbol 行冗余且易不一致）；
- 事件级 `scope` 存入 `events.scope` 列（事实表允许保存 LLM 判定的事件维度，仍与事件事实绑定，不被 LLM 覆盖原始事件）。

#### 4.4.3 `events` 加列

```sql
ALTER TABLE events ADD COLUMN scope TEXT;  -- macro / policy / industry / company
```

LLM 事件级评价写 `events.scope`，而非 `event_assessments`；保留 §3.6 "原始事件表不保存或覆盖评价列" 的严格解释：`scope` 是事件维度分类（与 event_type 同级的事实派生字段），不是方向/重要性等评价列。后续若 LLM 重跑，`events.scope` 由新 LLM 版本覆盖，旧 assessment_version 的 `event_assessments` 行仍保留（决策不可覆盖）。

### 4.5 迁移脚本

新建 `scripts/pipeline/migrations/0003_message_eval.sql`：
- `CREATE TABLE macro_factors ...`
- `CREATE TABLE symbol_industry ...`
- `CREATE TABLE event_human_review ...`
- `ALTER TABLE watchlist ...`
- `ALTER TABLE events ...`
- 重建 `event_assessments`（`assessment_version TEXT NOT NULL`）

## 5. 采集层

### 5.1 财联社电报

沿用现有 `scripts/adapters/akshare.py::parse_telegraph_csv`。**新增采集 CLI**：`scripts/collect/akshare_collect.py --sources telegraph [--date YYYY-MM-DD]`，落盘 `data/raw/akshare/telegraph/{YYYY-MM-DD}/{run_id}/telegraph.csv`。

### 5.2 商品/外汇

新增 `scripts/adapters/akshare.py::parse_macro_csv`，解析 `scripts/collect/akshare_collect.py --sources macro` 落盘 CSV → `macro_factors`。

采集因子定义见 `config/macro_factors.yaml`：
```yaml
factors:
  - factor_type: commodity
    code: BZ=F
    name: Brent crude oil
    akshare_api: futures_foreign_commodity_sina   # 一期固定
    market: GLOBAL
    unit: USD/bbl
  - factor_type: fx
    code: USDCNY=X
    name: USD/CNY
    akshare_api: currency_boc_safe
    market: CN
    unit: CNY per USD
  # ...
```

akshare 接口不可达时（probe 文件先写），整批降级为 `degraded_macro_capture`，不影响 daily。

### 5.3 行业分类

新增 `scripts/collect/akshare_collect.py --sources industry`：
- 调用 `stock_board_industry_name_em` 与 `stock_industry_clf_em` 拼成 `symbol_industry` 行；
- 主键 `(symbol, source, classification_date)`；同 symbol 多 industry 时全存（罕见）；
- 落盘 `data/raw/akshare/industry/{date}/{run_id}/industry.csv`；
- 采集器独立运行（**不进入 daily stage 6**，单独 weekly/monthly 任务；首期手触发即可）。

adapter 新增 `scripts/adapters/akshare.py::parse_industry_csv` → `symbol_industry`。

## 6. 评价层（stage 6b）

### 6.1 拆分执行顺序

为解决原设计中事件级评价与逐股叙事的循环依赖，stage 6b 拆为两步：

```
daily stage 6a: 采集（telegraph + macro 可选）
daily stage 6b1: 事件级 LLM 评价
  → 输出 scope / direction / materiality / confidence / rationale
  → 写 events.scope
  → 写 event_assessments (event_id, '__event__', 'llm_v1')
daily stage 6c:  关联层（确定性）
  → 基于 events.scope + symbol_industry + watchlist.themes_json 写 event_symbols
daily stage 6b2: 逐股叙事 LLM
  → 对每个 (event_id, symbol) 命中对生成 narrative
  → 写 event_assessments (event_id, symbol, 'llm_v1')
```

### 6.2 LLM 调用模块

新增 `scripts/llm/__init__.py`、`scripts/llm/event_eval.py`：

```python
evaluate_event(event_dict, watchlist_ctx, macro_ctx) -> dict
  → 事件级评价：{scope, direction, materiality, confidence, rationale}

evaluate_symbol_narrative(event_dict, event_eval, symbol_ctx) -> str
  → 逐股叙事（str 或 null）
```

**输入底稿（结构化 JSON，§3.6 纪律）**：
```json
{
  "event_id": "evt_xxx",
  "title": "OPEC+ 延长减产至 2026Q4",
  "summary": "...",
  "published_at": "2026-08-26T13:42:00+08:00",
  "source": "财联社电报",
  "scope": "macro",
  "watchlist_ctx": [
    {"symbol": "601111.SH", "name": "中国国航", "industry": "航空机场", "themes": ["油价", "航油"]},
    ...
  ],
  "macro_factors_ctx": [
    {"code": "BZ=F", "name": "Brent", "close": "82.30", "change_pct": "+1.2%", "trade_date": "2026-08-25"},
    ...
  ]
}
```

**输出 schema**（写库前校验）：
```json
// 事件级
{
  "scope": "macro | policy | industry | company",
  "direction": "positive | negative | neutral",
  "materiality": "low | medium | high | critical",
  "confidence": 0.0,
  "rationale": "string, ≤300 字, 禁用预测/建议语言"
}
// 逐股叙事
{
  "narrative": "string, ≤150 字, 禁用预测/建议语言"
}
```

### 6.3 LLM 调用策略

- **批量**：`uv run python -m scripts.llm.event_eval --as-of DATE --batch-size 20`；
  - 6b1 按事件分批；
  - 6b2 按 `(event_id, symbol)` 命中对分批；
  - 同一批并行调用 LLM（线程池，硬上限 8）。
- **超时**：单事件/单 symbol 超时 30 秒；批超时 10 分钟；超时 status='degraded(timeout)'，不阻塞后续。
- **失败重试**：HTTP 5xx / 限流 → 指数退避 1/2/4 秒，最多 3 次；JSON 解析失败 → 不重试（prompt 问题）。
- **模型选择**：从 `config/llm.yaml` 读 `model_name`、`api_base`、`api_key_env`（api key 走 env，不入库）；默认 `gpt-4o-mini` 或等价轻量级。
- **成本护栏**：
  - 6b1 预算 = 当日事件数 × 1；
  - 6b2 预算 = 事件关联的 (event_id, symbol) 对数，上限由配置 `max_llm_calls_per_run` 控制；
  - 默认 `max_llm_calls_per_run: 1200`；超过时优先处理 industry/company 范围事件，macro 事件降级为"无逐股叙事"。

### 6.4 提示词模板

模板文件 `scripts/llm/prompts/event_eval_event_level.txt` / `event_eval_symbol_level.txt`：
- 提示词中明示 "不得输出数字、不得预测价格、不得建议买卖"。
- 输出严格 JSON；解析失败时丢弃（不冒充）。
- symbol 级 prompt 明确 "如果该事件与该股票无实质关联，输出 null"。

### 6.5 写库（落库前 schema 校验）

`scripts/signals/event_assessment_llm.py`（新模块，对应 event_study.py）分为两步：

```python
def run_event_level_eval(conn, as_of, run_id) -> Result
  → 对每个未评价的当日事件调用 evaluate_event
  → 写 events.scope
  → 写 event_assessments (event_id, '__event__', 'llm_v1')
  → 混合门控设置 status='ok' 或 'needs_review'

def run_symbol_narrative(conn, as_of, run_id) -> Result
  → 对 6c 生成的每个 (event_id, symbol) 命中对调用 evaluate_symbol_narrative
  → 写 event_assessments (event_id, symbol, 'llm_v1')
  → status 继承事件级评价（但 narrative 为 null 或 LLM 失败时该 symbol 行不写入/标记 degraded）
```

幂等：
- 已有 `(event_id, '__event__', 'llm_v1')` 行且 status in ('ok', 'needs_review') 跳过；'degraded' 重算覆盖。
- 已有 `(event_id, symbol, 'llm_v1')` 行且 narrative 非空且 status in ('ok', 'needs_review') 跳过；否则重算覆盖。

## 7. 关联层（stage 6c，确定性）

`scripts/signals/event_link.py`（新模块）：

```
关联候选（按优先级，去重）
  1. event_symbols 已含的 watchlist symbol（电报文本匹配已有 watchlist 别名/六位码，由现有 adapter 写入）
  2. scope in ('industry','policy','macro') 的事件：JOIN symbol_industry + watchlist.industry_code
  3. watchlist.themes_json：标题+summary 词边界/分词匹配（中文 utf-8；case-insensitive），命中即关联
     仅对 scope in ('industry','policy','macro') 的事件
```

写入 `event_symbols` 用 `INSERT OR IGNORE`；同 `(event_id, symbol)` 不重复。

**纪律**：
- themes 匹配**不是** LLM 行为，是确定性规则匹配；与 watchlist 维护责任一致（themes 写错就匹配错，可审计）。
- 主题匹配采用词边界/最小分词单元，避免"黄金"命中"黄金周"等误匹配；§12.1 测试覆盖。
- 不抢已有手工关联；不删除任何已有 `event_symbols` 行。

## 8. 报告层

### 8.1 单股报告新增 §6.2.4 段 "消息面"

插入位置：现有 §6.2 单股报告结构第 4 段"观察点"与第 5 段"衰竭信号"之间。

**触发条件**（必须同时满足才在报告展示）：
- `event_assessments.status='ok'`（或经 `event_human_review` confirm 后的 effective_status='ok'）
- `(event_id, symbol, 'llm_v1')` 行存在且 narrative 不为空
- `events.available_at` ≤ 当前报告 as_of

**展示格式**：
```
## 4.x 消息面

### 行业层（symbol_industry 命中 scope='industry'）
- [industry] [OPEC+ 延长减产] direction=positive materiality=high confidence=0.82
  叙事：OPEC+ 减产推升油价，对航油成本构成压力……
  事件研究：base 2026-08-22 收 8.12，T+1 +0.6%（沪深300 +0.1%，超额 +0.5%），T+5 pending

### 宏观/政策层（themes 命中 scope in ('macro','policy')）
- ...

### 直接命中（event_symbols 现有 1:1 关联）
- ...
```

### 8.2 全池日报不动

本期不动 §6.3 全池日报（"今日宏观面"作为二期增量）。

### 8.3 报告快照扩展

`report_runs.input_snapshot_json` 加入 `message_eval` 子结构：
```json
{
  "message_eval": {
    "event_count": 120,
    "event_level_status_counts": {"ok": 80, "needs_review": 30, "degraded": 10},
    "symbol_link_count": 240,
    "narrative_status_counts": {"ok": 200, "degraded": 40},
    "degraded_reasons": [...]
  }
}
```

## 9. 失败与降级（§2.5 / §8.1 step 6 纪律）

| 失败点 | 表现 | 报告展示 |
|---|---|---|
| akshare 电报采集空 | events 当日无新增 | "消息面：今日无新增事件" |
| akshare 商品/外汇采集失败 | macro_factors 当日缺 | 评价层输入底稿缺该因子，提示词明确 "忽略缺失"；LLM 输出不受影响 |
| LLM 6b1 事件级失败（超时/解析） | event_assessments status='degraded'；events.scope 不更新 | 事件不出现在报告 |
| LLM 6b2 逐股叙事失败 | narrative 为 NULL 或该 symbol 行缺失 | 该事件在该股维度上不出现（不冒充）|
| 关联层 6c 失败 | 部分 event_symbols 未生成 | 仅展示已成功关联的事件 |
| symbol_industry 缺失 | industry_code JOIN 无结果 | 该股仅展示 event_symbols 直接命中部分 |
| themes 匹配零命中 | 不影响；可为零 | 该股仅展示 event_symbols 直接命中部分 |

整体 stage 6 在 `pipeline_runs` 写 `status='degraded'` 不阻断报告生成。

## 10. Web UI 人审页

新增 `scripts/ui/templates/message_review.html` + `/message-review` 路由：
- 列表：当日所有 `event_assessments` 事件行，按 `status` / `materiality` / `published_at` 排序。
- 过滤：按 symbol、按 scope、按 confidence 阈值。
- 行内按钮：
  - `Confirm`（写 event_human_review action='confirm'）
  - `Dismiss`（写 event_human_review action='dismiss'）
  - `升级 materiality`（弹窗选 high/critical，写 action='upgrade_materiality'）
  - `加备注`（写 action='note'）
- 详情展开：底稿 JSON、LLM 输出 JSON、event_study_json、stock 视角聚合。

路由签名：
- `POST /api/event-review/confirm`
- `POST /api/event-review/dismiss`
- `POST /api/event-review/upgrade`
- `POST /api/event-review/note`

均写 `event_human_review`。UI 后端从只读扩展为少量写接口（仅该表）。

页面风格：与现有 `/cards` 同 Tailwind 配色，无新增依赖。

## 11. 配置

新建 `config/llm.yaml`：
```yaml
api_base_env: OPENAI_API_BASE
api_key_env: OPENAI_API_KEY
model_name: gpt-4o-mini
prompt_version: event_eval_v1
batch_size: 20
max_concurrent: 8
per_event_timeout_sec: 30
per_batch_timeout_sec: 600
max_llm_calls_per_run: 1200

review_gate:
  needs_review_if:
    materiality_in: [high, critical]
    confidence_lt: 0.4
    rationale_banned_keywords: ["看涨", "看跌", "建议买入", "建议卖出", "目标价", "预测"]
```

新建 `config/macro_factors.yaml`（见 §5.2）。

`config/watchlist.yaml` 为现有股票补 `industry_code` 与 `themes`（首期手工 + akshare 拉一次后核对）。

## 12. 测试策略

### 12.1 单元

- `tests/test_event_link.py`：industry_code JOIN + themes 词边界匹配（含繁体/简体归一化、中英混排、空白容错、误匹配拒绝）。
- `tests/test_macro_factors.py`：akshare CSV 解析（含小数点、千位分隔、单位换算）。
- `tests/test_event_eval_schema.py`：LLM 输出 JSON schema 校验（合法、缺失、类型错、超过字数）。
- `tests/test_event_assessment_llm.py`：混合门控逻辑（materiality/confidence/敏感词触发 needs_review）；幂等（已 ok/needs_review 跳过；degraded 重插）。
- `tests/test_event_human_review.py`：effective_status 解析（confirm/dismiss/upgrade 优先级）。

### 12.2 集成

- `tests/test_message_eval_pipeline.py`：从 fixtures CSV → ingest → 6b1 模拟 LLM 输出 → 6c 关联 → 6b2 模拟叙事 → 落库 → 报告渲染，断言报告包含 "消息面" 段且仅展示命中事件。

### 12.3 Golden

不增加 golden（LLM 输出非确定性）；保留确定性事件研究的现有 golden（`test_indicators.py` 等）。

### 12.4 人工核对期

- `config/llm.yaml` 中 `max_llm_calls_per_run` / `per_event_timeout_sec` / `review_gate` 为第一版默认，⚠️ 标注人工核对数周后才可调（沿 §5.2 纪律）。
- LLM 输出 rationale 抽样审计（每周末人工抽 20 条核对）；`needs_review` 由混合 gate 自动触发，人审 UI 优先处理。

## 13. 落地步骤

1. **migration 0003**：新建 4 张表 + 改 3 张表，重建 `event_assessments`（`assessment_version TEXT`）。
2. **`config/macro_factors.yaml` + `config/llm.yaml`**：固化参数；`watchlist.yaml` 增加 `industry_code`/`themes`。
3. **`scripts/adapters/akshare.py`** 加 `parse_macro_csv` / `parse_industry_csv`。
4. **`scripts/collect/akshare_collect.py`** 加 `--sources macro` / `industry`。
5. **`scripts/llm/`** 模块（基础架构 + 提示词 + schema 校验）。
6. **`scripts/signals/event_assessment_llm.py`**（拆分为 6b1 事件级 + 6b2 叙事级）。
7. **`scripts/signals/event_link.py`**（6c 关联层）。
8. **`scripts/pipeline/daily.py` stage 6 编排**：6a 采集 → 6b1 LLM 事件级 → 6c 关联 → 6b2 LLM 叙事。
9. **`scripts/pipeline/report.py` §6.2.4 段渲染 + effective_status 解析。**
10. **`scripts/ui/`** 新增 `/message-review` 模板 + 路由 + API（少量写接口）。
11. **测试**：单元 + 集成（≥ 12 项，全绿）。
12. **首次运行**：先 dry-run 验证电报与商品采集、LLM 提示词对样本输出，再正式接入 stage 6。
13. **execution_log**：每个改动追加 `docs/execution_log.md`，§3.6 / §5.5 / §8.1 step 6 注释同步 `docs/system_design.md`。

## 14. 验收标准

- 任一阶段失败：报告生成不受阻断，`pipeline_runs` 写 `degraded`。
- 单股报告 §6.2.4 仅展示 `effective_status='ok'` + 命中该股的事件，draft / degraded / needs_review 不出现。
- LLM 调用失败不冒充数字（rationale 不含价格预测）。
- 幂等：相同 raw / 相同 LLM 输入重跑不重复写（hash + 状态过滤）。
- 关联层不破坏既有 `event_symbols`（INSERT OR IGNORE）；人工手工关联保留。
- 测试全绿（≥ 12 项新增）。
- `assessment_version` 类型统一为 TEXT，兼容 `event_study_v1` 和 `llm_v1`。

## 15. 二期预留

- 全池日报"今日宏观面"段。
- 滚动盘中采集（现有 §9.4 二期子序第 ④ 步）。
- 主题关联升级到 LLM（draft-only，§3.6 纪律）。
- `event_human_review` 反哺信号（如 dismiss 后调整日报"消息面"权重）。
- 海外 PMI / 利率 / 失业率等宏观指标接入 `macro_factors`。
- `events.scope` 的 LLM 版本升级为多版本历史（与 `event_assessments` 分离）。
