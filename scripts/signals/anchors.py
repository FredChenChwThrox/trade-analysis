"""周线锚点识别（D2.1，设计 §5.2）。

对每个 as_of（每个完成周）独立识别，只用该周及之前的数据（无未来函数，§5.1）：

- **恐慌低点**：最近一次有效恐慌型信号（exhaustion.panic_condition，含当前周）
  所在周内最低复权价的交易日；若没有恐慌型信号，fallback = 过去 lookback_weeks
  （默认 26）个完成周（含当前周）中最低复权收盘价所在周，并在周内定位最低复权价
  交易日。fallback 平值取离当前最近的一周（设计未规定，锁定于此）。
- **下跌起点**：恐慌低点周向前 lookback_weeks 个完成周内最高复权收盘价所在周；
  平值取离恐慌低点最近的一周（索引最大）。锚点交易日 = 该周周末交易日，
  锚点价格 = 该周复权收盘价（raw 取该日 close_raw）。
- 锚点身份 = (anchor_type, trade_date, is_fallback)。逐 as_of 重算时身份变化由
  调用方生成新 anchor_id（weekly_anchors 追加新行，不覆盖旧行，§5.2）。

daily 索引提供日线复权最低价/不复权价，用于"周内定位最低复权价交易日"；
锚点同时保存复权价（技术比较）与不复权价（排期卡价区口径，§3.4）。
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.signals.common import WeekBar
from scripts.signals.exhaustion import panic_condition

PANIC_LOW = "panic_low"
DECLINE_START = "decline_start"


@dataclass
class Anchor:
    anchor_type: str          # panic_low / decline_start
    week_index: int           # 锚点所在周在 weeks 序列中的索引
    trade_date: str           # 锚点交易日
    adjusted_price: float     # 识别时复权价（技术比较用）
    raw_price: float          # 当日不复权价（排期卡价区比较用）
    is_fallback: bool

    @property
    def key(self) -> tuple:
        return (self.anchor_type, self.trade_date, self.is_fallback)


@dataclass
class AnchorStep:
    """某个 as_of 周识别出的锚点对 + 写库后回填的 anchor_id。"""

    as_of: str                # week_end_date
    panic: Anchor
    decline: Anchor | None    # 恐慌低点周之前没有完成周时为 None
    panic_anchor_id: int | None = None
    decline_anchor_id: int | None = None


def _locate_week_low(week: WeekBar, week_index: int, daily: dict,
                     is_fallback: bool) -> Anchor:
    """周内定位最低复权价交易日；平值取最早交易日（锁定口径）。"""
    days = [
        (d, v) for d, v in daily.items()
        if week.week_start_date <= d <= week.week_end_date
    ]
    if not days:
        raise ValueError(
            f"周 {week.week_start_date}~{week.week_end_date} 无 daily_bars，"
            f"无法定位周内最低复权价交易日（先入库行情）")
    d, v = min(days, key=lambda kv: (kv[1]["low_adj"], kv[0]))
    return Anchor(PANIC_LOW, week_index, d, v["low_adj"], v["low_raw"], is_fallback)


def compute_anchor_timeline(
    weeks: list[WeekBar],
    daily: dict[str, dict],
    params: dict,
) -> list[AnchorStep]:
    """逐 as_of 识别锚点，返回与 weeks 等长的时间线（只用 <= as_of 的数据）。

    daily: {trade_date: {"low_adj", "low_raw", "close_raw"}}（复权/不复权日线）。
    """
    lookback = params["anchors"]["lookback_weeks"]
    steps: list[AnchorStep] = []
    last_panic_week: int | None = None
    for i, w in enumerate(weeks):
        if panic_condition(weeks, i, params):
            last_panic_week = i
        if last_panic_week is not None:
            j = last_panic_week
            panic = _locate_week_low(weeks[j], j, daily, is_fallback=False)
        else:
            lo = max(0, i - lookback + 1)  # 含当前周共 lookback 个完成周
            # 最低复权收盘价所在周；平值取离当前最近（索引最大）
            j = min(range(lo, i + 1), key=lambda k: (weeks[k].close, -k))
            panic = _locate_week_low(weeks[j], j, daily, is_fallback=True)

        decline: Anchor | None = None
        if j > 0:
            lo = max(0, j - lookback)  # 恐慌低点周向前 lookback 个完成周
            # 最高复权收盘价所在周；平值取离恐慌低点最近（索引最大）
            k = max(range(lo, j), key=lambda m: (weeks[m].close, m))
            wk = weeks[k]
            if wk.week_end_date not in daily:
                raise ValueError(
                    f"下跌起点周 {wk.week_end_date} 周末日无 daily_bars，"
                    f"无法取不复权收盘价")
            decline = Anchor(
                DECLINE_START, k, wk.week_end_date, wk.close,
                daily[wk.week_end_date]["close_raw"], panic.is_fallback)
        steps.append(AnchorStep(w.week_end_date, panic, decline))
    return steps
