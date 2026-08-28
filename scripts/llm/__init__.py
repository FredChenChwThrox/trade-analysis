"""LLM 评价链包（消息面 r2 Phase 3）。

模块：client（openai-compatible 调用）、prompts（提示词）、schema（输出校验）、
eval（6b1→6c→6b2 编排）。
铁律（r2 §6.2/AGENTS.md）：LLM 只消费结构化底稿、只产出严格 JSON；不产生规范化
数字、不预测价格、不建议买卖；输出解析失败即丢弃不冒充；人审 gate 见 eval。
"""
