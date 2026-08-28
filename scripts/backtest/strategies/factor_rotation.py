"""三因子 Top-N 周频轮动策略（计划 §10/§11/§12，Phase 3）。

架构：因子与交易分离——score 表（{date: {symbol: score}}）在引擎外由
factors.py 预计算，经工厂类属性注入；策略只做"取分→排序→等权调仓"。

纪律：
- 取分：触发 bar 当日严格取日期更早的最近一天分数（T-1 收盘信号），
  与 NextOpen 成交叠加双重隔离未来函数；
- 触发：每周首个被处理 bar 一次（ISO 周去重），当周无可用分数整周跳过；
- 等权 + CASH_BUFFER + rebalance_tolerance，同 Phase 2。
"""

from __future__ import annotations

from scripts.backtest.factors import select_scores_asof
from scripts.backtest.strategies.topn_rotation import (
    TopNRotationBase,
    _bar_date,
)


class FactorTopNBase(TopNRotationBase):
    """消费外部 score 表的周频 Top-N 等权轮动。

    注入项：universe_tuple（股票池）、score_map_tuple((date, {sym: score}), ...)
    由 make_factor_strategy 工厂构造。
    """

    def on_start(self):
        super().on_start()
        self.score_map = dict(self.score_map_tuple)

    def _pick_weights(self, date: str) -> dict[str, float] | None:
        """按 T-1 分数选 Top-N 并返回等权目标表；无分返回 None。"""
        scores = select_scores_asof(self.score_map, date)
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        picks = [s for s, _ in ranked[: self.params.top_n]]
        weight = (1.0 - self.CASH_BUFFER) / len(picks)
        return {s: weight for s in picks}

    def on_bar(self, bar):
        d = _bar_date(bar.timestamp)
        iso = (d.isocalendar().year, d.isocalendar().week)
        if iso == self._iso_week:
            return
        self._iso_week = iso
        weights = self._pick_weights(d.isoformat())
        if not weights:
            return
        self.rebalance_weights(
            target_weights=weights,
            liquidate_unmentioned=True,
            rebalance_tolerance=0.01,
        )


def make_factor_strategy(universe: list[str],
                         score_map: dict[str, dict[str, float]],
                         ) -> type[FactorTopNBase]:
    """生成绑定股票池与外部评分表的策略类。"""
    return type("FactorTopN", (FactorTopNBase,), {
        "universe_tuple": tuple(universe),
        "score_map_tuple": tuple(sorted(score_map.items())),
    })
