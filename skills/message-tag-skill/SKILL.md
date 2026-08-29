---
name: message-tag-skill
description: 消息面打标签 skill（消息面研判 r2 Phase 3）。对未评价的消息事件（公告/新闻）按"四道筛子"产出结构化标签 draft（scope/direction/materiality/confidence/target/half_life/预期差/证伪建议/rationale + 可选逐股叙事），导入后进 /message-review 人工复核。触发语：对消息面事件打标 / 消息面标签 / 评价一下今天的事件。draft-only：标签必须经人工复核才进报告。
---

# 消息面打标签 Skill（draft-only，人工复核必经）

## 铁律（违反即废稿）

1. **不产生任何数字预测**：不预测价格、涨跌幅、目标价、时间点；
2. **不建议买卖/加减仓**：action_hint 只是"该消息一般触发哪类流程"的提示；
3. 禁用词：必涨、必跌、建议买入、建议卖出、目标价、涨停、抄底、逃顶（命中 → status 会被置 needs_review）；
4. 标签是 **draft**：导入后 status 按人审 gate 落定，人工在 /message-review 可确认/否决/改写；
5. 信息不足 → confidence ≤ 0.3 并在 expectation_gap 写明缺口，**不猜**。

## 流程

1. **导出底稿**（未评价事件 + 宏观因子背景，JSONL）：

   ```bash
   uv run python -m scripts.llm.inputs export --as-of <交易日> --limit 20
   # → data/llm_tags/<交易日>_input.jsonl
   ```

2. **逐条打标**：读每行底稿（title/summary/来源 tier/宏观因子背景）。公告类
   （event_type=announcement）scope 恒为 company；新闻按内容判
   macro/policy/industry/flow，判不了 给 industry 并在 expectation_gap 说明。
   每条输出一个 JSON 对象（一行）：

   ```json
   {"event_id": "evt_xxxx", "scope": "industry", "direction": "negative",
    "materiality": "medium", "confidence": 0.7, "target": "eps",
    "half_life": "week", "expectation_gap": null,
    "action_hint": "none", "falsification_suggestion": null,
    "rationale": "≤300字影响路径陈述（规则语言，不预测）",
    "narratives": [{"symbol": "601899.SH", "narrative": "≤150字该股影响，无关则不给"}]}
   ```

   - source_tier / event_type **由 import 以 events 表为准**，标签行不用写；
   - confidence ∈ [0,1] 数字；rationale ≤300 字；narrative ≤150 字且仅当
     事件与该股相关才给；
   - 四道筛子（r2 §2.2）：①信源质量看 tier；②预期差（无一致预期数据就 null）；
     ③target 改 EPS 还是改 PE；④half_life 时效半衰期。

3. **导入**：

   ```bash
   uv run python -m scripts.llm.inputs import --tags data/llm_tags/tags.jsonl \
       --actor <你的名字> --as-of <交易日>
   ```

   非法行会被拒绝（不冒充）；公司公告 tier≤2 自动 needs_review；
   model 记 `agent:<actor>` 可追溯。

4. **提示人工复核**：打开 /message-review（消息审页），逐条确认/否决/改写；
   ok + 有叙事的才会出现在单股报告"### 消息面"段。

## 事实边界

- 事件事实（标题/摘要/来源/tier）以 events 表为准，不得虚构补充事实；
- 需要原文时公告 canonical_url 可查（巨潮/交易所 PDF 为准，成色词"拟/原则上"
  要人工判）；
- 宏观因子背景（macro_context）仅用于判断环境，不要在 rationale 里复述。
