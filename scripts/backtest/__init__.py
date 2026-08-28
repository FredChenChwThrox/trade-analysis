"""AKQuant 回测子包（与监测管线 scripts/pipeline 隔离，只读 market.db）。

隔离原则：
- 本子包不 import scripts/pipeline / scripts/adapters / scripts/indicators；
- 数据库连接为本子包自带的只读实现（scripts/backtest/db.py），不写库；
- 回测参数入 config/backtest.yaml，不读 signals.yaml/indicators.yaml。
"""
