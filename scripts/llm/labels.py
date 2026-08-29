"""标签中文呈现（消息面 r2 Phase 3）。

DB 存英文枚举（存储契约，测试与打标通道都用英文值）；本模块是唯一的
展示层映射：报告"### 消息面"与 /message-review 人审页统一从这里取中文，
杜绝各处散落的硬编码翻译。
"""

from __future__ import annotations

DIRECTION_CN = {"positive": "利好", "negative": "利空", "neutral": "中性"}
MATERIALITY_CN = {"low": "轻微", "medium": "一般", "high": "重大",
                  "critical": "关键"}
TARGET_CN = {"eps": "盈利（EPS 底稿）", "pe": "估值（锚/风险偏好）",
             "sentiment": "仅情绪面"}
HALF_LIFE_CN = {"day": "日级（数日失效）", "week": "周级（数周失效）",
                "month": "月级（数月失效）", "quarter": "季度级"}
ACTION_HINT_CN = {"none": "不触发流程", "swing": "波段确认时参考",
                  "schedule": "触发排期卡复核", "redraw_anchor": "提示重画估值锚"}
STATUS_CN = {"ok": "已过审", "needs_review": "待人审"}


def cn(mapping: dict, value, dash: str = "不涉及") -> str:
    """枚举 → 中文；None → dash（默认"不涉及"）；未知值原样返回（不猜）。"""
    if value is None:
        return dash
    return mapping.get(value, value)


def tags_line(view: dict) -> str:
    """报告用：把 effective 显示值 dict 渲染为一行中文标签。"""
    conf = view.get("confidence")
    conf_s = f"把握 {conf:.0%}" if conf is not None else "把握 —"
    return (f"方向 {cn(DIRECTION_CN, view.get('direction'), '—')}"
            f" ｜ 重要性 {cn(MATERIALITY_CN, view.get('materiality'), '—')}"
            f" ｜ {conf_s}"
            f" ｜ 作用 {cn(TARGET_CN, view.get('target'))}"
            f" ｜ 半衰期 {cn(HALF_LIFE_CN, view.get('half_life'))}"
            f" ｜ 流程 {cn(ACTION_HINT_CN, view.get('action_hint'))}")
