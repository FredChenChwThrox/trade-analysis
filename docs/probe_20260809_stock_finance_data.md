# 数据源实测记录：stock_finance_data（2026-08-09）

> D1.1 产出。测试标的：珀莱雅 603605.SH，区间 2023-08-09 ~ 2026-08-08。
> 原始文件：`data/raw/stock_finance_data/price/2026-08-09/run_probe01/`

## 字段勘察（get_price，interval=D）

- 返回列：`open, high, low, close, volume, thscode, time, thsname_cn, thsname_en, currency`
- 726 行（3 年），无空值；`time` 为 YYYYMMDD 整数；volume 单位为股。
- **`amount`（成交额）缺失** → `daily_bars.amount_raw` 允许为空（符合设计降级约定）。
- 无直接复权因子列 → 按设计 §3.3 反推：`来源因子 f_t = 前复权价 ÷ 不复权价`。

## 复权因子反推验证（adjust=none vs adjust=forward 各采 3 年）

- volume 两口径完全一致（前复权不动成交量），share_factor 需另行处理。
- 逐日比值存在 ±0.0001 级抖动（两侧价格均保留 2 位小数的舍入噪声），
  **adapter 必须做平台段检测（窗口中位数/四舍五入到 3~4 位），不能按逐日 diff 判除权**。
- 因子平台段清晰可见，3 年共 5 次除权（均为小额现金分红，share_factor=1）：

| 除权生效日 | 因子段（f_t） | 幅度 |
|---|---|---|
| 2023-10-23 | 0.9446 → 0.9484 | ≈0.4% |
| 2024-06-25 | 0.9484 → 0.9558 | ≈0.8% |
| 2025-06-17 | 0.9558 → 0.9696 | ≈1.4% |
| 2025-10-17 | 0.9696 → 0.9794 | ≈1.0% |
| 2026-07-22 | 0.9794 → 1.0000 | ≈2.1% |

- 内部后复权因子换算：`price_adj_factor_t = f_t / f_origin`（origin 日归一为 1.0，见设计 §3.3）。
- 增量策略修订点：前复权序列在每次除权后全历史变化，故增量采集时
  **前复权只需采重叠窗口（5 个交易日）做因子比对**；发现平台段位移才全量重采前复权重建因子。
  因子变化检测阈值建议 0.1%（相对），远大于舍入噪声、远小于最小分红幅度（0.4%）。

## 对 adapter 的输入约定

- 字段映射：`open/high/low/close → *_raw`，`volume → volume_raw`，`amount_raw = NULL`，
  `time → trade_date`，`currency` 原样。
- 2026-08-07（周五）数据已返回，T 日收盘当日可采。
