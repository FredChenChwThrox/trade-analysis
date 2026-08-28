"""提示词（消息面 r2 Phase 3）。

系统提示固化铁律：不产生数字、不预测价格、不建议买卖、只输出严格 JSON。
底稿只喂结构化事实（事件字段 + 宏观因子快照），不喂价格序列/技术指标原文。
prompt_version 见 config/llm.yaml（写入 event_assessments.prompt_version 可追溯）。
"""

from __future__ import annotations

SYSTEM_RULES = """你是 A 股消息面研判助手，为人工复核产出结构化标签。
铁律（违反即废稿）：
1. 不得输出任何数字预测：不预测价格、涨跌幅、目标价、时间点；
2. 不得建议买入/卖出/加减仓；action_hint 只是"该消息一般会触发哪类流程"的提示，不是操作建议；
3. 不得使用以下词汇：必涨、必跌、建议买入、建议卖出、目标价、涨停、抄底、逃顶；
4. 只输出一个严格 JSON 对象，无任何多余文字、无 markdown 围栏；
5. rationale ≤300 字，用"触发/证伪/复核/释放仓位/待确认"类规则语言，基于底稿陈述事实与影响路径；
6. 信息不足时把 confidence 降到 0.3 以下并在 expectation_gap 写明缺口，不猜。"""

_EVENT_FIELDS = """字段口径（四道筛子，r2 §2.2）：
- scope: macro(货币/财政/海外流动性) / policy(监管/补贴/集采/税) / industry(行业景气/协会数据) / company(财报/回购/激励/诉讼/分红) / flow(龙虎榜/两融/大宗/解禁)
- direction: positive / negative / neutral（对该事件指向的基本面方向）
- materiality: low / medium / high / critical
- target: eps(改盈利底稿) / pe(改估值与风险偏好) / sentiment(仅情绪) / null
- half_life: day / week / month / quarter（消息时效半衰期）
- expectation_gap: 与一致预期的偏离方向与幅度；无一致预期数据时写 null
- action_hint: none / swing(波段确认) / schedule(排期卡复核触发) / redraw_anchor(估值锚重画提示)
- falsification_suggestion: 证伪条件建议稿（人工定稿），无则 null
- rationale: ≤300 字影响路径陈述"""


def event_prompt(event: dict, macro_lines: list[str]) -> tuple[str, str]:
    """事件级初判（6b1）。event 为事件底稿 dict，macro_lines 为宏观因子快照行。"""
    user = (
        "事件底稿：\n"
        f"{event['published_at'][:10]} [{event['event_type']}/tier{event.get('source_tier')}] "
        f"{event['title']}\n摘要：{event.get('summary') or '（无）'}\n\n"
        + ("宏观因子背景（最近快照，仅供判断环境，不要复述）：\n"
           + "\n".join(macro_lines) + "\n\n" if macro_lines else "")
        + "请输出事件级评价 JSON。\n" + _EVENT_FIELDS
    )
    return SYSTEM_RULES, user


def narrative_prompt(event: dict, symbol: str, industry_name: str | None) -> tuple[str, str]:
    """逐股叙事（6b2）：同一事件在该股维度的 ≤150 字影响陈述。"""
    user = (
        "事件底稿：\n"
        f"{event['published_at'][:10]} {event['title']}\n摘要：{event.get('summary') or '（无）'}\n\n"
        f"目标股票：{symbol}" + (f"（{industry_name}）" if industry_name else "") + "\n\n"
        "请输出该事件对这只股票的影响叙事 JSON：{\"narrative\": string|null}。\n"
        "narrative ≤150 字；只说影响路径与观察点（如：影响哪条业务、传导快慢、"
        "该看什么数据确认），不预测价格、不给买卖建议；与该股无关则写 null。"
    )
    return SYSTEM_RULES, user
