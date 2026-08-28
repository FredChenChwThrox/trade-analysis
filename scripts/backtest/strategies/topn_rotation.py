"""Top-N 周频等权轮动策略（计划 §9/§12 简化版，Phase 2 验证用）。

纪律：
- 信号时点：每周首个被处理 bar（收盘后触发 on_bar），仅每周一次；
- 数据可见性：get_history 只返回当前 bar 之前的数据，横截面取分时
  其他标的的当日 bar 尚未入缓冲，评分统一"截至上一收盘"，无未来函数；
- 成交：run_backtest 默认 NextOpen()，T 日信号 T+1 开盘成交；
- 目标仓位用 rebalance_weights（文档确认接口），等权 + liquidate 未提及持仓。

隔离：不 import 本仓库其他 scripts/*。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from akquant import IntParam, Strategy

_CN_TZ = ZoneInfo("Asia/Shanghai")


def _bar_date(ts_ns: int):
    """bar 的 UTC ns 时间戳 → 市场本地日期。"""
    return datetime.fromtimestamp(
        ts_ns / 1_000_000_000, tz=timezone.utc).astimezone(_CN_TZ).date()


class TopNRotationBase(Strategy):
    """周频动量 Top-N 等权轮动。

    universe 经 make_topn_strategy 工厂以类属性注入（避免依赖构造器参数语义）。
    """

    top_n = IntParam(5, ge=1, le=50)
    lookback = IntParam(20, ge=5, le=250)
    # 现金缓冲：目标权重总和 = 1 - CASH_BUFFER，吸收滑点/佣金/跳空造成的
    # "后成交腿资金差一点"拒单（同双均线 95% 目标的同一问题域）。
    # 5% 实测把 18 只周频轮动的 margin 拒单从 6 笔压到个位乃至零；
    # 引擎对残余拒单次日过期、下周按实际权益重定目标，天然自愈。
    CASH_BUFFER = 0.05
    # rebalance_tolerance 固定 1%：分数小幅波动不引发高换手（文档推荐实践）

    def on_start(self):
        self.warmup_period = self.params.lookback + 1
        self.universe = list(getattr(self, "universe_tuple", ()))
        self._iso_week = None

    def _momentum_scores(self) -> dict[str, float]:
        """各股 lookback 收盘动量；样本不足的跳过（§2.5 明示由 CLI 报告）。"""
        scores: dict[str, float] = {}
        n = self.params.lookback
        for sym in self.universe:
            closes = self.get_history(count=n, symbol=sym, field="close")
            if len(closes) < n:
                continue
            base = float(closes[0])
            if base <= 0:
                continue
            scores[sym] = float(closes[-1]) / base - 1.0
        return scores

    def on_bar(self, bar):
        d = _bar_date(bar.timestamp)
        iso = (d.isocalendar().year, d.isocalendar().week)
        if iso == self._iso_week:
            return  # 每周只调仓一次
        self._iso_week = iso

        scores = self._momentum_scores()
        if not scores:
            return  # warmup 内整周跳过
        ranked = [s for s, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]
        picks = ranked[: self.params.top_n]
        weight = (1.0 - self.CASH_BUFFER) / len(picks)
        self.rebalance_weights(
            target_weights={s: weight for s in picks},
            liquidate_unmentioned=True,
            rebalance_tolerance=0.01,
        )


def make_topn_strategy(universe: list[str]) -> type[TopNRotationBase]:
    """生成绑定股票池的策略类（类属性注入，实例化安全）。"""
    return type("TopNRotation", (TopNRotationBase,), {"universe_tuple": tuple(universe)})
