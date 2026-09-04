---
name: message-tag-skill
description: 消息面打标签 skill（消息面研判 r2 Phase 3）。对未评价的消息事件（公告/新闻）按"四道筛子"产出结构化标签 draft（scope/direction/materiality/confidence/target/half_life/预期差/证伪建议/rationale + 可选逐股叙事），导入后进 /message-review 人工复核。触发语：对消息面事件打标 / 消息面标签 / 评价一下今天的事件 / 最近一周的消息补标。draft-only：标签必须经人工复核才进报告。
---

# 消息面打标签 Skill（draft-only，人工复核必经）

## 铁律（违反即废稿）

1. **不产生任何数字预测**：不预测价格、涨跌幅、目标价、时间点；
2. **不建议买卖/加减仓**：action_hint 只是"该消息一般触发哪类流程"的提示；
3. 禁用词：必涨、必跌、建议买入、建议卖出、目标价、涨停、抄底、逃顶
   （出现在 rationale → status 置 needs_review）；
4. 标签是 **draft**：导入后 status 按 gate 自动落定，人工在 /message-review
   确认/否决/改写后才生效；
5. 信息不足 → confidence 如实给低分（<0.4 会被 gate 强制人审，压低分数不会
   "直通"），并在 expectation_gap 写明缺口，**不猜**；
6. **看不懂先查再打**：一眼看不出影响的事件（陌生公司/冷门政策/专业术语），
   先搜索（WebSearch 等）弄清背景——公司主业、行业位置、政策指向——再判；
   查完仍不明 → confidence 给 <0.4 并在 expectation_gap 写明缺口。
   你打标时看不懂的，人审同样看不懂：搜索查证是打标工序的一部分，不是可选项。
   rationale 要**自解释**：把查到的背景浓缩进影响路径陈述，格式如
   `"XX 公司主营 YY 业务，本轮 ZZ 政策影响其上游成本环节；若落地将压低毛利率，需跟踪细则执行口径"`——人审不查原文也能看懂"这是谁、什么事、为什么重要"。

## 人审 gate 全规则（r2 §6.3，导入时自动判定，命中任一 → needs_review）

- `materiality` ∈ {high, critical}；
- `confidence` < 0.4；
- `rationale` 含禁用词；
- `scope` = company 且 `source_tier` ≤ 2（按 scope 判定，公告和新闻都适用）。

都不命中 → ok。needs_review 不进报告"### 消息面"段，等人工处置；打标前应对
自己的标签会落哪个 status 有预期。

## 三种打标场景

| 场景 | export 参数 | 输出路径 |
|---|---|---|
| 日常增量（默认全池最新 N 条） | `--as-of <交易日> --limit 20` | `data/llm_tags/<交易日>_input.jsonl` |
| 个股深查（只看该股关联事件） | `--as-of <交易日> --symbol 601088.SH` | `..._<交易日>_<股票>_input.jsonl` |
| 批量补标（"最近一周"类） | `--as-of <交易日> --start <起始日> --limit 500` | 同上 |

已评价（llm_v1 存在）的事件不会重复导出；宏观因子背景（macro_context）为全
市场快照，三种模式都带。

## 流程

### 1. 导出底稿（未评价事件 + 宏观因子背景，JSONL）

```bash
uv run python -m scripts.llm.inputs export --as-of <交易日> --limit 20
```

每行字段：`event_id / event_type / title / summary / published_at / source /
source_tier / linked_symbols / canonical_url / macro_context`。

### 2. 逐条打标

每条输出一个 JSON 对象（一行）。tags 文件命名约定
`data/llm_tags/<as-of>_tags.jsonl`（批量补标可加语义前缀，如 `week_tags.jsonl`）：

```json
{"event_id": "evt_xxxx", "scope": "industry", "direction": "negative",
 "materiality": "medium", "confidence": 0.7, "target": "eps",
 "half_life": "week", "expectation_gap": null,
 "action_hint": "none", "falsification_suggestion": null,
 "rationale": "≤300字影响路径陈述（规则语言，不预测）",
 "narratives": [{"symbol": "601899.SH", "narrative": "≤150字该股影响，无关则不给"}]}
```

- `source_tier` / `event_type` **由 import 以 events 表为准**，标签行不用写
  （多写的键会被归一化丢弃）；
- 长度/取值约束（schema 强制）：confidence ∈ [0,1]；rationale ≤300 字；
  expectation_gap / falsification_suggestion ≤200 字；narrative ≤150 字；
- rationale 要**自解释**：人审者不查原文也能看懂影响路径——把查证到的背景
  （这是谁、什么事、为什么重要）浓缩进影响路径陈述；查证不到的写"待人工核"，
  不留半句只有自己看得懂的话；
- narratives 仅当事件与该股相关才给，且 **symbol 必须在底稿 linked_symbols
  内**（否则报告关联不上，等于白写）；
- scope 判定：公告类（event_type=announcement）恒为 company；新闻按内容判
  macro/policy/industry/flow，判不了给 industry 并在 expectation_gap 说明；
- direction 无明显倾向给 neutral，不必强行站队。

**四道筛子判定口径**（r2 §2.2，打标时逐条过）：

1. **信源质量**：看 source_tier——1 公告/交易所原文、2 官媒/部委原文、3 券商
   研报、4 财经媒体、5 自媒体/热度榜（tier 5 只做情绪参考，不进决策链）。
   tier 数字越大，confidence 越该保守；
2. **预期差**：与一致预期的偏离方向。无一致预期数据 → expectation_gap 置
   null 不编；有"符合预期"迹象也写明（符合预期的好消息常是出货窗口）；
3. **target**：改 EPS（业绩/订单/成本 → eps）还是改 PE（风险偏好/叙事/流动性
   → pe）；只影响情绪给 sentiment；判不了给 null；
4. **half_life**：政策拐点 → quarter；业绩/订单 → month；舆情/资金流 → day；
   介于其间 → week。半衰期决定这条消息有资格驱动哪一档流程。

action_hint 口径：`none` 无需动作 / `swing` 波段确认类 / `schedule` 可能触发
排期档 / `redraw_anchor` 估值锚可能需重画。

### 3. 导入

```bash
uv run python -m scripts.llm.inputs import --tags data/llm_tags/<as-of>_tags.jsonl \
    --actor <你的名字或模型标识> --as-of <交易日>
```

- schema 校验非法行**拒绝不冒充**（终端打印前 10 条拒绝原因）；narrative 非法
  （如超 150 字）**单条丢弃**，事件标签本身照常入库；
- **幂等**：已评价（llm_v1 存在）的事件导入时跳过——被拒/被丢弃的行改完可
  直接重导，不会重复写库；
- model 记 `agent:<actor>` 可追溯打标主体。

### 4. 提示人工复核

打开 /message-review（消息审页），逐条确认/否决/改写；effective = ok 且有叙事
的事件才会出现在单股报告"### 消息面"段。

## 对照示例

能过 gate（industry + tier 3 + medium + 0.7，rationale 无禁用词 → ok）：

```json
{"event_id": "evt_a", "scope": "industry", "direction": "positive",
 "materiality": "medium", "confidence": 0.7, "target": "eps",
 "half_life": "month", "expectation_gap": "无一致预期数据，需人工对照研报",
 "action_hint": "none", "falsification_suggestion": "下月协会数据库存回升即证伪",
 "rationale": "上游库存连续两月下行，若延续将改善行业盈利预期", "narratives": []}
```

会进 needs_review 的写法（任一即命中）：confidence 0.35、materiality high、
rationale 出现"建议买入"、company + tier 1。

## 事实边界

- 事件事实（标题/摘要/来源/tier/canonical_url）以 events 表为准，不得虚构补充事实；
- 需要原文时用底稿里的 `canonical_url` 查（巨潮/交易所 PDF 为准，成色词
  "拟/原则上"要人工判）；
- 搜索只用于**理解背景**：事件本身的细节（金额/日期/主体/成色）以底稿为准，
  搜到的数字不得当作事件事实写进标签；
- 宏观因子背景（macro_context）仅用于判断环境，不要在 rationale 里复述；
- linked_symbols 是采集期的确定性关联；发现漏关联/错关联不要在标签里硬补
  事实，在 expectation_gap 写明、留给人审处置。
