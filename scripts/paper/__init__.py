"""模拟盘（信号决策力实验）——决策录入/结算/统计。

设计：docs/superpowers/specs/2026-09-05-paper-trading-design.md（v2 评审修订版）。

**与 backtest 的边界**（评审四.7）：机械基线为本模块内置朴素实现
（同决策点集"信号即全跟"），**不复用、不调用** `scripts/backtest/`（akquant 路径），
两套对照口径互不影响。

定位纪律：与 executions 完全隔离；不进 daily 信号链；决策人工录入（自动化=没有
判断可测）；LLM 不参与。反作弊五防线见设计 §2.3。
"""
