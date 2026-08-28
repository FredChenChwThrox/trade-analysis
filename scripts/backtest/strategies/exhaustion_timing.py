"""衰竭信号时序事件策略（Phase A：排期卡择时层的忠实机械化）。

入场（释放条件，语义=纯信号时序版，放弃卡片价区半条件）：
    持仓为 0 且 ≤T 的最近完成周"同 anchor_id active 衰竭信号 ≥ min_signals"
    → 目标仓位买入。
出场（机械证伪线，代理人工卡线）：
    收盘 ≤ 最近 decline_start 锚点的 HFQ 止损位 → 清仓；
    锚点在持有期内更新时止损位随之切换到新锚。

注入项由工厂类属性提供：
    events_map_tuple: ((week_end, n_active), ...)
    weeks_tuple:      (完成周日期, ...)          # 已验证完成周
    anchors_tuple:    ((trade_date, stop_adj|None, adj_ref), ...)  # decline_start 升序

无未来函数三重保障：信号周事实生产端逐周点时计算；本层只取 ≤T 完成周；
成交 NextOpen()。所有比较在 HFQ 域完成（feed 与 stop 同口径）。
"""

from __future__ import annotations

from bisect import bisect_right

from scripts.backtest.strategies.topn_rotation import (
    TopNRotationBase,
    _bar_date,
)


class ExhaustionTimingBase(TopNRotationBase):
    min_signals = 2            # 同锚 active 衰竭数门槛（卡片口径）
    entry_discount_pct = 0.0   # 入场价折扣门：close ≤ 锚点adj×(1-x)；0=关闭（纯信号版）

    def on_start(self):
        super().on_start()
        self.week_counts = dict(self.events_map_tuple)   # {week: (anchor_id, n)}
        self.weeks = list(self.weeks_tuple)
        self.anchors = list(self.anchors_tuple)          # [(trade_date, stop|None)]
        self._anchor_idx = -1
        self._entry_anchor = None     # 当前持仓对应的 episode 锚；离场即清

    # ---- 事件侧 ----
    def _week_event_asof(self, date_iso: str):
        """→ (anchor_id, n_active)；无完成周或该周无事实 → (None, 0)。"""
        if not self.weeks or self.weeks[0] > date_iso:
            return None, 0
        wk = self.weeks[bisect_right(self.weeks, date_iso) - 1]
        aid, n = self.week_counts.get(wk, (None, 0))
        return aid, n

    def _advance_anchor(self, date_iso: str) -> None:
        nxt = self._anchor_idx
        while (nxt + 1 < len(self.anchors)
               and self.anchors[nxt + 1][0] <= date_iso):
            nxt += 1
        self._anchor_idx = nxt

    def _current_stop(self) -> float | None:
        """最近一个带有效止损位的锚（回退跳过缺因子的锚）。"""
        idx = self._anchor_idx
        while idx >= 0:
            stop = self.anchors[idx][1]
            if stop is not None:
                return stop
            idx -= 1
        return None

    def _current_anchor_ref(self) -> float | None:
        """最近锚的后复权参考价（入场折扣门基准）。"""
        if self._anchor_idx < 0:
            return None
        ref = self.anchors[self._anchor_idx][2]
        return ref

    def _current_anchor_id(self) -> int | None:
        """当前生效锚的稳定序号（以 trade_date 序代替 id，锚可跨重算复用）。"""
        return self._anchor_idx

    # ---- 交易侧 ----
    def on_bar(self, bar):
        d = _bar_date(bar.timestamp).isoformat()
        prev_idx = self._anchor_idx
        self._advance_anchor(d)

        pos = self.get_position(bar.symbol)

        # 出场①：机械证伪线（HFQ 域）
        if pos > 0:
            # 归属未知（引擎初始化时序边角）：先认领当前锚，不误触发终结
            if self._entry_anchor is None:
                self._entry_anchor = self._anchor_idx
            stop = self._current_stop()
            if stop is not None and bar.close <= stop:
                self.close_position(symbol=bar.symbol)
                self._entry_anchor = None
                return
            # 出场②：episode 终结——持有期内锚推进到入场锚之后
            if (prev_idx != self._anchor_idx
                    and self._anchor_idx > self._entry_anchor):
                self.close_position(symbol=bar.symbol)
                self._entry_anchor = None
                return
        else:
            # 入场：最近完成周同锚 active≥min，且本 episode 未开过仓；
            # 锚切换（新 episode）后自动解锁。
            aid, n = self._week_event_asof(d)
            cur_aidx = self._current_anchor_id()
            gate_zone = True
            disc = getattr(self, "entry_discount_pct", 0.0)
            if disc > 0:
                ref = self._current_anchor_ref()
                gate_zone = ref is not None and bar.close <= ref * (1 - disc)
            if (aid is not None and n >= self.min_signals
                    and gate_zone
                    and cur_aidx is not None and cur_aidx != self._entry_anchor):
                self.order_target_percent(
                    symbol=bar.symbol, target_percent=0.95)
                self._entry_anchor = cur_aidx


def make_exhaustion_strategy(events_map: dict[str, int],
                             weeks: list[str],
                             anchors: list[tuple[str, float | None]],
                             entry_discount_pct: float = 0.0,
                             ) -> type[ExhaustionTimingBase]:
    """生成绑定事件表的策略类（工厂注入，实例化安全）。"""
    ns = {
        "events_map_tuple": tuple(sorted(events_map.items())),
        "weeks_tuple": tuple(weeks),
        "anchors_tuple": tuple(anchors),
    }
    if entry_discount_pct:
        ns["entry_discount_pct"] = float(entry_discount_pct)
    return type("ExhaustionTiming", (ExhaustionTimingBase,), ns)
