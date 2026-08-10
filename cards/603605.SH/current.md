<!-- 当前 active 视图，自动刷新自 2026-08-10_603605SH_120ca661.md；勿手工编辑（§2.4） -->

# 排期卡 603605.SH — 603605SH_120ca661

> 本文件由 `strategy_card_versions` 库记录渲染，仅作存档视图，不手工回写数据库（§2.4）。

## 版本信息

- symbol: 603605.SH
- card_version_id: `603605SH_120ca661`
- status: active（schema_version=card_v1）
- created_at: 2026-08-10T01:28:21.162810+00:00
- 生效区间: [2026-08-10, 开口)（排他端点）
- supersedes_id: —
- currency: CNY / price_basis: raw（价区为不复权绝对价位）
- next_review_at: 2026-08-31（到期生成复核提醒，不自动延后）
- run_id: card_activate_603605SH_120ca661

## 盈利情景（EPS）

- bear: 2.85
- base: 3.56
- bull: 3.90

## 估值情景（PE 刻度）

- neutral: 18 / optimistic: 25 / pessimistic: 15
- 刻度样本区间: {'from': '2023-08-09', 'note': 'PE 刻度为 3 年样本（设计 §3.2 强制标注），引用更早历史视为样本外', 'to': '2026-08-07'}（§3.2：3 年样本强制标注）

## 三档价区（不复权）

| 档 | 下沿 | 上沿 | 触发附加条件 |
|---|---|---|---|
| T1 | 61.50 | 64.10 | 无 |
| T2 | 54.70 | 57.60 | 同一锚点活跃衰竭信号 ≥ 2 项 |
| T3 | 42.80 | 46.20 | 同一锚点活跃衰竭信号 ≥ 2 项 |

## 证伪线

- line: 42.80（收盘有效跌破=极悲情景被击穿（连续 2 日收盘低于线 1%），冻结仓位重做基本面判断，禁止继续摊低成本）
- 有效跌破口径: 收盘 ≤ 线 ×(1−1%) 连续 2 个交易日（config/signals.yaml）

## 波段箱体（只监测存档边界）

- 箱体下沿: 54.70
- 箱体上沿: 61.00
- 买区下沿: 54.00
- 买区上沿: 56.50
- 卖区下沿: 59.50
- 卖区上沿: 61.00
- 箱体证伪: 54.70

## 右侧确认

- 触发位: 61.00 / 止损位: 59.50
- 状态机: 收盘突破触发位 1% 且量 ≥ 前 20 日均量 2 倍 → 等待回踩；10 个交易日内回踩 ±2% 且收盘守住 −1% → confirmed（config/signals.yaml）

## 输入快照


```json
{
  "created_via": "fred-valuation-card-skill 交互生成（首张真实卡）",
  "data_cutoff": "2026-08-07",
  "demo": false,
  "earnings_basis": {
    "forecast_gap": "FY1 一致预期 16.26 亿（+8.59%）vs 实际趋势 -6.05%，裂口 14.64pp 未收敛，情景以实际趋势为准",
    "latest_quarter": "2026Q1 归母 3.67 亿，同比 -6.05%",
    "ttm_eps": 3.7228,
    "ttm_net_profit_yi": 14.74
  },
  "eps_scenario_detail": {
    "base_neutral": {
      "assumption": "实际趋势外推：2026Q1 同比 -6.05%",
      "eps": "3.56"
    },
    "bear": {
      "assumption": "中性 -10%（盈利持续温和下滑）",
      "eps": "3.20"
    },
    "bull_recovery": {
      "assumption": "裂口收敛后回到 FY2025 盈利水平",
      "eps": "3.90"
    },
    "worst": {
      "assumption": "中性 -20%（大单品失速/渠道库存恶化）",
      "eps": "2.85"
    }
  },
  "exhaustion_params": {
    "active_signals_now": [
      "duration"
    ],
    "dryup_volume_range": [
      6461200,
      9691700
    ],
    "front_low_raw": 61.25,
    "note": "当前活跃信号仅 1 项，第二、三档未释放",
    "panic_volume_threshold": 32305800,
    "vol_base_shares": 16152900
  },
  "matrix_source": "build_schedule.py --eps 3.56,3.20,2.85 --pe 25,18,15 --price 57.72",
  "review_triggers": "2026 中报披露（next_review_at 2026-08-31）；毛利率异常下滑/费用失控/现金流背离净利立即复核",
  "right_side_notes": "触发位 61.00=箱体上沿，止损 59.50（跌回上沿下方约 2.5%）；与左侧仓/波段仓分开记账，触发前不动",
  "swing_box_notes": "用户确认箱体 54.7-61（已实盘 2 次波段）；买入区下沿 54.00 低于箱体证伪线 54.70——按纪律收盘有效跌破下沿无条件退出，54.00-54.70 区间买入存在刚买即退出的张力，用户知情确认；仓位上限 20% 独立记账",
  "win_rate_estimate": {
    "note": "现价≈悲观EPS×中性PE（已定价下滑）基准 55-60%；盈利下滑 -5、裂口未收敛 -5~-10、衰竭信号 +5（仅 1 项）、体系切换风险 -5~-10；区间宽度 >15pp=证据不足，按固定比例下限执行，Kelly 上限本版不出数",
    "range": "35%-55%"
  }
}
```
