"""消息面打标包（r2 Phase 3）。

打标走 **agent/skill 通道**：scripts/llm/inputs.py（export 底稿 → agent 按
skills/message-tag-skill 打标 → import 校验入库，产出 llm_v1 行，过人审 gate）。
原 API 自动通道（zhipu chat completions）已于 2026-08-28 移除（执行日志续⑭）：
打标是研判判断，agent 现场质量优于单次 API 调用，且免除 key/成本/对接负担。
"""

from __future__ import annotations
