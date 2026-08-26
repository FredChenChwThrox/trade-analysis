# 消息面/宏观对监控列表股价影响分析（D3 消息评价）

> 状态：设计稿（待 review）
> 关联文档：`docs/system_design.md`（§3.6 公告与新闻、§5.5 消息评价与事件研究、§8.1 每日盘后阶段 6、§9.4 上线顺序 D3）、`docs/database_schema.md`（§6 events / event_assessments）、`AGENTS.md`（LLM 受限参与）。
> 取代：暂无（首版）。

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
- 不引入 LLM 评价 draft-only 激活机制（一期 status 直生效，status='ok' 才入报告）。

## 2. 总体架构

```
采集层（akshare）
  ├─ 财联社电报       → events / event_symbols              (已有 adapter，扩展)
  ├─ 商品快照         → macro_factors                       (新 adapter)
  └─ 行业分类         → symbol_industry                     (新 adapter，一次性 + 季度刷新)

评价层（LLM，stage 6b）
  ├─ 事件级评价       → event_assessments
  │                     (event_id, symbol='__event__', 'llm_v1')
  └─ 逐股叙事         → event_assessments
                        (event_id, symbol, 'llm_v1')

关联层（Python，stage 6c，确定性）
  ├─ 直接命中：event_symbols 已含的 watchlist 股票
  └─ 行业命中：symbol_industry JOIN industry_code
                把 scope in ('industry','policy','macro') 的事件
                关联到 industry_code 命中的 watchlist 股票
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
- **§3.6 draft-only 纪律例外**：本功能**一期不设** LLM draft 激活；`event_assessments.status='ok'` 即生效，'needs_review' / 'degraded' 在报告中不出现。
- **§6.4 语言纪律**：评价 rationale 不允许出现"看涨/看跌/建议买入"等预测语言；提示词模板强制约束。
- **§8.3 幂等**：
  - events 入库沿用现有 source_external_id + content_hash 去重。
  - macro_factors 主键 `(factor_type, code, trade_date)`，相同值跳过。
  - event_assessments 落库前检查 `(event_id, symbol, assessment_version)` 是否已存在；存在且 status='ok' 跳过；其他状态重算覆盖。
  - industry → watchlist 关联层写 `event_symbols` 用 `INSERT OR IGNORE`，不破坏现有手工关联。
- **§9.5 硬门槛**：raw 落盘 + content_hash + run 记录；JSON 列写库前校验 schema；金额/价格/汇率用定点。
- **§9.5 软约束**：本设计不改变既有的单字段 Decimal 降级做法。

## 4. 数据模型变更

### 4.1 新表：`macro_factors` [事实]

```sql
CREATE TABLE macro_factors (
    factor_type  TEXT NOT NULL,        -- commodity / fx / index_proxy
    code         TEXT NOT NULL,        -- e.g. 'BZ=F' (Brent), 'AU0' (gold SHFE), 'CNYHKD=X'
    name         TEXT NOT NULL,        -- 人读名：'Brent crude oil', 'SHFE gold', 'CNY/HKD'
    market       TEXT NOT NULL,        -- CN / GLOBAL
    trade_date   TEXT NOT NULL,        -- 市场本地日期
    close        TEXT NOT NULL,        -- 定点 TEXT
    change_pct   TEXT,                 -- 定点 TEXT（与前一日比）
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

**采集策略**：一次性全量同步 + 季度定时刷新；akshare `stock_board_industry_name_em` + `stock_industry_clf` 组合获取每只股票的行业码。同一 symbol 多源并存时 `eastmoney` 优先。

### 4.3 新表：`event_human_review` [决策]

人审状态与审计字段；不修改 `event_assessments`，避免覆盖 LLM 原始产出。

```sql
CREATE TABLE event_human_review (
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    symbol              TEXT NOT NULL,                -- '__event__' or 股票 symbol
    assessment_version  TEXT NOT NULL,                -- 'llm_v1'
    action              TEXT NOT NULL,                -- 'dismiss' / 'upgrade_materiality' / 'note'
    payload_json        TEXT NOT NULL,                -- JSON：变更详情
    actor               TEXT NOT NULL,
    reviewed_at         TEXT NOT NULL,                -- UTC
    PRIMARY KEY (event_id, symbol, assessment_version, action)
);
```

`dismiss` 后，事件从"今日相关"列表中对该 symbol 排除（仅对当前评估版本生效；LLM 重跑后失效）。`upgrade_materiality` 写入"high/critical"覆盖显示；不写回 `event_assessments`，报告渲染时 join 优先取人审结果。

### 4.4 既有表变更

#### 4.4.1 `watchlist` 加列 [配置]

```sql
ALTER TABLE watchlist ADD COLUMN industry_code TEXT;
ALTER TABLE watchlist ADD COLUMN themes_json TEXT;     -- JSON 数组，e.g. '["油价","航油","汇率"]'
```

- `industry_code`：从 `symbol_industry` 反查覆盖；不在 `symbol_industry` 的股票 `industry_code` 为 NULL（仅靠 `event_symbols` 直接命中）。
- `themes_json`：扩展事件匹配；`scope=macro` 事件标题/内容命中 themes 关键词时也尝试关联（draft-only 纪律——LLM 关联建议只写 event_symbols 时，仍需脚本不写入，需人工复阅。但本期跳过 LLM 关联——主题关联由 themes + Python 简单子串匹配做，LLM 不参与，匹配结果直接写 `event_symbols`，与 watchlist 维护责任一致）。

> 注：本期 LLM **不参与** 个股关联推断，所有个股→事件关联由 Python 确定性派生（直接命中 watchlist 别名/六位码 + industry_code JOIN + themes 子串匹配）。LLM 仅产出"为什么这件事对该股相关"的叙事。

#### 4.4.2 `event_assessments` 加列（轻量）[决策]

```sql
ALTER TABLE event_assessments ADD COLUMN scope TEXT;
ALTER TABLE event_assessments ADD COLUMN narrative TEXT;     -- 逐股叙事（仅 symbol!='__event__' 行填）
ALTER TABLE event_assessments ADD COLUMN model TEXT;          -- 已有但补 NULL 兜底（数据库 schema 注释提到 0002 已知 INTEGER 亲和问题）
```

注：scope 是事件级字段；但为每行存一份无意义浪费。本设计选择在 `__event__` sentinel 行存 scope，`narrative` 在 symbol 行存。

#### 4.4.3 `events` 不改

LLM 评价写 `event_assessments` 而非 `events`，保留 §3.6 "原始事件表不保存或覆盖评价列" 纪律。

### 4.5 迁移脚本

新建 `scripts/pipeline/migrations/0003_message_eval.sql`：
- `CREATE TABLE macro_factors ...`
- `CREATE TABLE symbol_industry ...`
- `CREATE TABLE event_human_review ...`
- `ALTER TABLE watchlist ...`
- `ALTER TABLE event_assessments ...`

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
- 调用 `stock_board_industry_name_em` 与 `stock_industry_clf` 拼成 `symbol_industry` 行；
- 主键 `(symbol, source, classification_date)`；同 symbol 多 industry 时全存（罕见）；
- 落盘 `data/raw/akshare/industry/{date}/{run_id}/industry.csv`；
- 采集器独立运行（**不进入 daily stage 6**，单独 weekly/monthly 任务；首期手触发即可）。

adapter 新增 `scripts/adapters/akshare.py::parse_industry_csv` → `symbol_industry`。

## 6. 评价层（stage 6b）

### 6.1 LLM 调用架构

新增 `scripts/llm/__init__.py`、`scripts/llm/event_eval.py`：

```
evaluate_event(event_dict, watchlist_ctx) -> dict
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
  "confidence": 0.0~1.0,
  "rationale": "string, ≤300 字, 禁用预测/建议语言"
}
// 逐股叙事
{
  "narrative": "string, ≤150 字, 禁用预测/建议语言"
}
```

### 6.2 LLM 调用策略

- **批量**：`uv run python -m scripts.llm.event_eval --as-of DATE --batch-size 20`；按事件分批；同一批事件并行调用 LLM（线程池，硬上限 8）。
- **超时**：单事件超时 30 秒；批超时 10 分钟；超时事件 status='degraded(timeout)'，不阻塞后续。
- **失败重试**：HTTP 5xx / 限流 → 指数退避 1/2/4 秒，最多 3 次；JSON 解析失败 → 不重试（prompt 问题）。
- **模型选择**：从 `config/llm.yaml` 读 `model_name`、`api_base`、`api_key_env`（api key 走 env，不入库）；模型出 `gpt-4o-mini` 或等价轻量级。
- **成本护栏**：单次 daily 调用预算上限（默认 200 个事件 × 1 个事件级 + 5 个 symbol 级 = 1200 次）。可在 `config/llm.yaml` 配。

### 6.3 提示词模板

模板文件 `scripts/llm/prompts/event_eval_event_level.txt` / `event_eval_symbol_level.txt`：
- 提示词中明示 "不得输出数字、不得预测价格、不得建议买卖"。
- 输出严格 JSON；解析失败时丢弃（不冒充）。

### 6.4 写库（落库前 schema 校验）

`scripts/signals/event_assessment_llm.py`（新模块，对应 event_study.py）：
- 输入：`events ⋈ event_symbols ⋈ events`，过滤当日 `published_at` 区间 + available_at ≤ as_of。
- 事件级：每个 event_id 一行 `(event_id, '__event__', 'llm_v1')`。
- 逐股叙事：每个 `(event_id, symbol)` 在关联层命中后一行 `(event_id, symbol, 'llm_v1')`。
- 幂等：已有 `(event_id, symbol, 'llm_v1')` 行 status='ok' 跳过；其他状态 DELETE 重插。

## 7. 关联层（stage 6c，确定性）

`scripts/signals/event_link.py`（新模块）：

```
关联候选（按优先级，去重）
  1. event_symbols 已含的 watchlist symbol（电报文本匹配已有 watchlist 别名/六位码，由现有 adapter 写入）
  2. scope in ('industry','policy','macro') 的事件：JOIN symbol_industry + watchlist.industry_code
  3. watchlist.themes_json：标题+summary 子串匹配（中文 utf-8；case-insensitive），命中即关联
```

写入 `event_symbols` 用 `INSERT OR IGNORE`；同 `(event_id, symbol)` 不重复。

**纪律**：
- themes 匹配**不是** LLM 行为，是确定性子串匹配；与 watchlist 维护责任一致（themes 写错就匹配错，可审计）。
- 主题匹配不抢已有手工关联；不删除任何已有 `event_symbols` 行。

## 8. 报告层

### 8.1 单股报告新增 §6.2.4 段 "消息面"

插入位置：现有 §6.2 单股报告结构第 4 段"观察点"与第 5 段"衰竭信号"之间，或作为第 4 段子节（视报告模板结构）。

**触发条件**（必须同时满足才在报告展示）：
- `event_assessments.status='ok'`
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

`report_runs.input_snapshot_json` 加入 `message_eval` 子结构（事件数、status 分布、degraded 原因），可追溯。

## 9. 失败与降级（§2.5 / §8.1 step 6 纪律）

| 失败点 | 表现 | 报告展示 |
|---|---|---|
| akshare 电报采集空 | events 当日无新增 | "消息面：今日无新增事件" |
| akshare 商品/外汇采集失败 | macro_factors 当日缺 | 评价层输入底稿缺该因子，提示词明确 "忽略缺失"；LLM 输出不受影响 |
| LLM 事件级失败（超时/解析） | event_assessments status='degraded' | 事件不出现在报告 |
| LLM 逐股叙事失败 | narrative 为 NULL | 该事件在该股维度上不出现（不冒充）|
| symbol_industry 缺失 | industry_code JOIN 无结果 | 该股仅展示 event_symbols 直接命中部分 |
| themes 子串匹配零命中 | 不影响；可为零 | 该股仅展示 event_symbols 直接命中部分 |

整体 stage 6 在 `pipeline_runs` 写 `status='degraded'` 不阻断报告生成。

## 10. Web UI 人审页

新增 `scripts/ui/templates/message_review.html` + `/message-review` 路由：
- 列表：当日所有 `event_assessments` 事件行，按 `status` / `materiality` / `published_at` 排序。
- 过滤：按 symbol、按 scope、按 confidence 阈值。
- 行内按钮：`Dismiss`（写 event_human_review action='dismiss'）、`升级 materiality`（弹窗选 high/critical，写 action='upgrade_materiality'）、`加备注`。
- 详情展开：底稿 JSON、LLM 输出 JSON、event_study_json、stock 视角聚合。

路由签名：`POST /api/event-review/dismiss` 等，写 `event_human_review`。

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
daily_call_budget: 1200
```

新建 `config/macro_factors.yaml`（见 §5.2）。

`config/watchlist.yaml` 为现有 17 只补 `industry_code` 与 `themes_json`（首期手工 + akshare 拉一次后核对）。

## 12. 测试策略

### 12.1 单元

- `tests/test_event_link.py`：industry_code JOIN + themes 子串匹配纯函数（含繁体/简体归一化、中英混排、空白容错）。
- `tests/test_macro_factors.py`：akshare CSV 解析（含小数点、千位分隔、单位换算）。
- `tests/test_event_eval_schema.py`：LLM 输出 JSON schema 校验（合法、缺失、类型错、超过字数）。
- `tests/test_event_assessment_write.py`：幂等（已 status='ok' 跳过；其他状态重插）；与现有 `event_study.py` 互不干扰（不同 assessment_version 命名空间）。

### 12.2 集成

- `tests/test_message_eval_pipeline.py`：从 fixtures CSV → ingest → link → 模拟 LLM 输出 → 落库 → 报告渲染，断言报告包含 "消息面" 段且仅展示命中事件。

### 12.3 Golden

不增加 golden（LLM 输出非确定性）；保留确定性事件研究的现有 golden（`test_indicators.py` 等）。

### 12.4 人工核对期

- `signals.yaml` / `llm.yaml` 中 `daily_call_budget` / `per_event_timeout_sec` 为第一版默认，⚠️ 标注人工核对数周后才可调（沿 §5.2 纪律）。
- LLM 输出 rationale 抽样审计（每周末人工抽 20 条核对）；`status='needs_review'` 由 LLM 自评低置信度触发（confidence < 0.4 或 rationale 含敏感关键词），人审 UI 优先处理。

## 13. 落地步骤

1. **migration 0003**：新建 4 张表 + 改 2 张表，写库函数。
2. **`config/macro_factors.yaml` + `config/llm.yaml`**：固化参数。
3. **`scripts/adapters/akshare.py`** 加 `parse_macro_csv` / `parse_industry_csv`。
4. **`scripts/collect/akshare_collect.py`** 加 `--sources macro` / `industry`。
5. **`scripts/llm/`** 模块（基础架构 + 提示词 + schema 校验 + 落库）。
6. **`scripts/signals/event_assessment_llm.py`**（stage 6b 主体）。
7. **`scripts/signals/event_link.py`**（stage 6c 关联层）。
8. **`scripts/pipeline/daily.py`** stage 6 编排：6a 采集 → 6b LLM → 6c 关联。
9. **`scripts/pipeline/report.py`** §6.2.4 段渲染。
10. **`scripts/ui/`** 新增 `/message-review` 模板 + 路由 + API。
11. **测试**：单元 + 集成（≥ 12 项，全绿）。
12. **首次运行**：先 dry-run 验证电报与商品采集、LLM 提示词对样本输出，再正式接入 stage 6。
13. **execution_log**：每个改动追加 `docs/execution_log.md`，§3.6 / §5.5 / §8.1 step 6 注释同步 `docs/system_design.md`。

## 14. 验收标准

- 任一阶段失败：报告生成不受阻断，`pipeline_runs` 写 `degraded`。
- 单股报告 §6.2.4 仅展示 status='ok' + 命中该股的事件，draft / degraded / needs_review 不出现。
- LLM 调用失败不冒充数字（rationale 不含价格预测）。
- 幂等：相同 raw / 相同 LLM 输入重跑不重复写（hash + 状态过滤）。
- 关联层不破坏既有 `event_symbols`（INSERT OR IGNORE）；人工手工关联保留。
- 测试全绿（≥ 12 项新增）。

## 15. 二期预留

- 全池日报"今日宏观面"段。
- 滚动盘中采集（现有 §9.4 二期子序第 ④ 步）。
- 主题关联升级到 LLM（draft-only，§3.6 纪律）。
- `event_human_review` 反哺信号（如 dismiss 后调整日报"消息面"权重）。
- 海外 PMI / 利率 / 失业率等宏观指标接入 `macro_factors`。
