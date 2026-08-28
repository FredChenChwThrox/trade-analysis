"""AKQuant 策略实现（Phase 1）。

隔离：本目录只依赖 akquant，不 import 本仓库其他 scripts/*。
"""

from __future__ import annotations

import numpy as np

from akquant import IntParam, Strategy


class DualMAStrategy(Strategy):
    """双均线策略：短均线上穿长均线 → 目标仓位买入；下穿 → 清仓。

    参数内联声明（IntParam），口径对照计划 §6：
    - short_window 短均线窗口
    - long_window 长均线窗口
    - target_pct 买入目标仓位
    无未来函数：on_bar 内 get_history 只返回当前 bar 之前的数据；
    warmup_period 保证长窗样本齐前不触发交易逻辑。
    """

    short_window = IntParam(5, ge=2, le=200)
    long_window = IntParam(20, ge=3, le=500)
    target_pct = IntParam(95, ge=1, le=100)  # 0.95 目标仓位

    def on_start(self):
        self.warmup_period = self.params.long_window

    def on_bar(self, bar):
        closes = self.get_history(
            count=self.params.long_window,
            symbol=bar.symbol,
            field="close",
        )
        if len(closes) < self.params.long_window:
            return

        short_ma = float(np.mean(closes[-self.params.short_window:]))
        long_ma = float(np.mean(closes))

        position = self.get_position(bar.symbol)
        if short_ma > long_ma and position == 0:
            self.order_target_percent(
                symbol=bar.symbol,
                target_percent=self.params.target_pct / 100.0,
            )
        elif short_ma < long_ma and position > 0:
            self.close_position(symbol=bar.symbol)
