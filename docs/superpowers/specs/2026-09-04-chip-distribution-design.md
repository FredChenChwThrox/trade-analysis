# 自算筹码分布设计方案（换手率衰减模型）v2

> 状态：**v2 已按评审意见修订**（v1 评审结论：conditional approve，2026-09-04；
> 修订映射见 §0.1，可直接进入实施或二次评审）
> 日期：2026-09-04（v1 同日，v2 评审修订）
> 关联：§5.7 筹码集中度缺口；执行日志 2026-09-04（换手率+股东户数落地，本方案的输入已就位）
> 定位：**模型估算观察项**——与东财 cyq 同性质的估算数据，不进信号链、不进卡片触发（§2.5）

---

## 0. 一句话

用已回填的 3 年 OHLC + 成交量 + 换手率（25524 行，2026-09-04 就位），以标准换手率衰减模型
Python 自算逐日筹码分布，产出获利比例/平均成本/90% 成本区间/集中度四项，落
`chip_distribution` 快照表，全部数字确定性可测、参数可审计。

### 0.1 v1 → v2 修订映射（评审意见对照）

| # | 评审意见 | v2 处置 | 位置 |
|---|---|---|---|
| 1 | 明确复权域现金分红偏差 | §2.3 加偏差声明 + §7 加"不还原真实股东成本" | §2.3/§7 |
| 2 | A=1.0 过高，改 0.6–0.7 | **A=0.7 默认**，k_cap 重新定位为异常数据护栏 | §2.1/§4.3 |
| 3 | burn_in=60 偏松（残留公式纠错） | **burn_in=90 默认**，残差公式显式写入 | §3 |
| 4 | 三角核 peak=均价依据不足，涨停日失真 | **默认 close 峰**，vwap 峰/均匀核降为对照参数 | §2.2/§4.3 |
| 5 | rule_version 体现核形状 | rule_version=`chip_v1_close_tri`（形状/峰值编码进版本串） | §4.2 |
| 6 | 存 amount_used | 表加 `amount_used` 列（对照核审计快照） | §4.2 |
| 7 | 补测试：除权连续性/高换手截断/次新首日/折回反验 | §5 新增 4 组用例 | §5 |
| 8 | adjust.py 提示 chip 需重算 | §4.1 + §9 任务 7（只加 NOTE 提示，不耦合事务） | §4.1/§9 |
| 9 | 折回反向验证 | §5.1 性质测试纳入 `avg_cost_raw × factor == avg_cost_adj` | §5 |

## 1. 背景与动机

1. 东财 `stock_cyq_em` 依赖 push2his.eastmoney.com——本机直连被断、代理被拒（2026-09-04
   实测 ProxyError + 代理出口 RemoteDisconnected），且属间歇性风控；其数据本身也是东财
   自用衰减模型估算，参数不透明。
2. Tushare Pro cyq_perf/cyq_chips 需新增供应商（账号+积分），同样是黑箱模型。
3. 模型所需输入已全部在库内（本系统自有数据），自算 = 零外部依赖 + 算法透明可测 +
   参数可审计，与"自算历史 PB 序列"（§9.4 先例）同一哲学。**本设计不需要调用任何
   东财接口**——全部输入来自 akshare-sina 日线（turnover 100% 回填）与 adjust.py 因子。
4. **定位纪律**：无论自算还是外采，筹码分布都是模型估算值，只作观察项（报告展示/基本面
   底稿引用），永不进信号链与排期卡触发（§2.5 不猜、LLM 边界）。本方案不改变这一定位。

## 2. 模型定义

### 2.1 换手率衰减模型（逐日递推）

对每只股票，在**复权价格域**维护一个筹码分布直方图 `w(p)`（p 为价格网格点，w 为权重）：

**初始化**（窗口首日 d₀）：全部筹码均匀分布在 d₀ 当日价格区间 [low_d₀, high_d₀]：

```
w₀(p) = 1 / (n_bins_in_range)   ∀ p ∈ [low_d₀, high_d₀]；否则 0
```

**逐日递推**（d = d₀+1, …, T）：

```
k_d = min(A × turnover_d, k_cap)        # 当日换手强度（衰减比例）
若 d 停牌或 turnover 缺失 → k_d = 0（不衰减不新增，分布原样延续）
w_d(p) = (1 − k_d) × w_{d−1}(p) + k_d × B_d(p)
```

**参数语义（v2 修订）**：

- `A = 0.7`（默认）：换手率 × 0.7 才是当日被实际替换的筹码比例。**依据**：现实中
  100% 换手不可能 100% 替换存量筹码（大股东、长持者不动），A=1.0 是最激进假设，
  会系统性高估新筹码占比；0.6–0.7 是行业同类模型的经验区间（东财/通达信不公开参数，
  但量级与此一致）。⚠️ 第一版默认值，待东财恢复后交叉验证再定（§6）。
- `k_cap = 0.8`：**异常数据护栏**，不是常规约束。A=0.7 下 k_cap 触发需
  turnover ≥ 1.143（114%+，正常行情不会出现），仅拦截 turnover 数据异常（如错报
  5.0）导致旧筹码一夜清零。A=0.7 下 k(turnover) 在常规域内平滑无拐点
  （k(0.79)=0.553、k(0.81)=0.567），v1 评审指出的"非单调区"随 A 修正自然消失。

### 2.2 当日新增筹码分布核 B_d（v2：close 峰三角，默认）

```
峰值价  peak_d = close_d（复权口径收盘价）
支撑区间 [low_d, high_d]（复权口径）
B_d(p) = 三角形密度：peak 处最高，线性下降至 low/high 处为 0，积分为 1
边界情形：
  一字板（low == high == close）→ 退化为点分布（质量全在该价位）——正确
  涨停/跌停收盘（close == high 或 == low）→ 峰在区间边界，三角退化为直角三角形，
  质量天然集中在涨停/跌停价附近——与"封板日成交集中于板价"的事实一致，
  这正是选 close 峰而非均价峰的主要理由之一
```

**为什么 close 峰而不是 vwap 峰（vwap = amount/volume，v1 默认，评审否决）**：

1. **涨停日失真**：v1 的 vwap 峰在封板日把新增筹码质量放在均价而非板价，与真实成交
   集中位置相反；close 峰自然落在板价。
2. **低数据量稳定性**：close 是当日市场共识定价，vwap 依赖 amount/volume 两列的口径
   一致性（尤其 amount 历史缺口行，2026-08-27 回填前有 42 个边界行缺失）。
3. **可解释性**：close 峰下模型叙事唯一（"当日按收盘价附近换手"），无第二套口径。

线性下降（三角）vs 尖峰肥尾（log-normal/beta）的取舍：v1 数据条件下三角核参数最少、
边界行为最可控（尖峰族需要额外形状参数且在一字板上同样退化）；先以对照参数保留
（§4.3），交叉验证后若有系统性形状偏差再议。

### 2.3 复权处理（v2：补现金分红偏差声明）

**全程在复权价格域计算**（p_adj = raw × price_adj_factor，§3.3），输出时按 §5.4 折回
不复权口径：

```
avg_cost_raw = avg_cost_adj ÷ factor_d
cost_5/95_raw 同式折回
winner_ratio 用复权 close 比较（比例无量纲，factor 约掉；
  数学上 close_adj/avg_cost_adj ≡ close_raw/avg_cost_raw）
```

**⚠️ 口径偏差声明（评审意见 #1，必须与结果一起读）**：

`price_adj_factor` 是"前复权价相对不复权价"的累积因子（adjust.py 平台段口径），
**现金分红除权也会让历史价格（进而历史筹码成本）向下平移**。而真实世界中现金分红
并不平移股东的实际持股成本（资金占用/税务/心理成本不变），只有送转/拆股这类股数
变化才"自然"降低每股成本。因此：

- 分红除权后，本模型的 avg_cost 会**系统性低估**真实股东成本，分红越多越频繁偏差越大
  （池内高分化工/银行股受影响相对更大）；
- 这是前复权口径的固有属性，与全项目其他前复权指标（MA 折回等）一致，不是本模型
  新引入的问题；它与东财/通达信估算的差异也部分来源于此；
- v1 **不做**现金分红与送转除权的差异化处理（复杂度不值，见 §7），如未来需要
  "真实成本口径"，应基于 corporate_actions 明确拆分两类事件后重建因子——另立项。

**折回一致性验证**（纳入回归测试，评审意见 #9）：对每个输出日断言
`avg_cost_raw × factor_d ≈ avg_cost_adj`（数值容差 1e−9 相对误差）。

### 2.4 输出指标（逐日四项 + 状态）

| 指标 | 定义 | 备注 |
|---|---|---|
| winner_ratio | Σw(p), p ≤ close_d_adj ÷ Σw(p) | 获利比例 [0,1] |
| avg_cost | Σ(p·w(p)) ÷ Σw(p)（复权域算，折回输出） | 平均成本 |
| cost_5 / cost_95 | w 累积到 5% / 95% 分位的价格（同上折回） | 90% 成本区间两沿 |
| concentration_90 | (cost_95 − cost_5) ÷ (cost_95 + cost_5) | 90% 集中度，东财同式 |
| estimation_status | burn_in / mature / insufficient_data | 见 §3（v2：'ok' 改名 'mature'） |

### 2.5 数值实现

- **价格网格**：复权域固定分辨率。每股取窗口内 [min(low_adj)×0.9, max(high_adj)×1.1]，
  等宽 `n_bins=2000` 格；全部指标由网格直方图聚合（分位数用累积权重线性插值）。
  2000 格 × 750 日 × 34 只，单股重算 <1s，全池 <40s（纯 numpy 向量化，无需优化）。
- **网格外价格**：若某日 high_adj 超出初始化网格上界（历史新高），超界部分质量按
  最近 bin 归并，`estimation_status` 该日不降级（网格余量 10% + 等比重建兜底：
  若超界 >1% 网格宽，全股按扩展域重建一次）。v1 从简，评审问题 6 的处置。
- **停牌日**（trading_status='suspended' 或无 bar）：跳过，分布原样延续。
- **turnover 缺失**：k_d=0（不衰减不新增）——2026-09-04 已 100% 回填，此路径仅为防御。
- **因子重建**：adjust.py 全量重建因子后，筹码分布须重算（依赖 price_adj_factor
  与 turnover 都在 daily_bars，天然幂等，见 §4.1 幂等语义与提示机制）。

## 3. Burn-in（v2：90 日 + 残差公式显式化）

窗口首日"全部筹码均匀铺在当日区间"是强假设：现实中的筹码继承自窗口之前。初始权重
残留按几何衰减：

```
residual(d) ≈ (1 − A × avg_turnover)^d
```

**数值对照（评审纠错后的正确数字，v1 的 20% 估算有误）**：

| 假设 | 60 日残留 | 90 日残留 | 120 日残留 |
|---|---|---|---|
| A=1.0，日均换手 2% | 29.8% | 16.2% | 8.8% |
| **A=0.7，日均换手 2%** | **42.6%** | **28.1%** | **18.6%** |
| A=0.7，日均换手 3% | 28.4% | 14.2% | 7.1% |

处置（评审建议方案一）：

- `burn_in_days = 90`（默认，⚠️ 待核对）：前 90 日 `estimation_status='burn_in'`，
  报告/底稿引用必须带标注；
- 90 日后状态为 `'mature'`——**"可引用"不等于"无偏"**：A=0.7 下 90 日仍有 ~28% 初始
  残留（高换手股更快收敛），该口径偏差在跨股比较时方向一致，相对排序仍可用；
- 不截断数据（保留序列连续性），只打标。

## 4. 落地设计

### 4.1 代码位置与 CLI

```
scripts/indicators/chip_distribution.py    # 模型 + CLI（indicators 层，同 compute.py 风格）
  uv run python -m scripts.indicators.chip_distribution <symbol> [--as-of D]
  uv run python -m scripts.indicators.chip_distribution --all     # 全池重算
```

- 纯函数 `compute_chip_series(days, params) -> list[dict]`（便于 golden tests，同
  right_side.evaluate_segment 模式）
- 幂等：`DELETE FROM chip_distribution WHERE symbol=?` + 重插 + `pipeline_runs`
  记录（rule_version 见 §4.2，config_hash 随 params 入库）——§6 派生表惯例
- `--all` 生成**单一全局 run_id** 共享（同 daily 管线惯例），各股行内冗余存
  run_id/rule_version/config_hash/computed_at（与 indicators_daily 风格一致）
- **不进 daily.py 默认链**（手触发/按需，同 balance_sheet 先例）：观察项 + 全池 40s
  不宜进每日关键路径。是否后续并入 daily 由人工决定。
- **与 adjust.py 的联动（评审意见 #8）**：不在 adjust.py 事务内耦合 chip 重算
  （避免拉长除权重建关键路径）；在 `AdjustResult.notes` 增加一条提示——检测到因子
  重建时输出 `NOTE: chip_distribution 需重算（python -m scripts.indicators.chip_distribution --all）`，
  由人工/后续流程触发。任务清单见 §9 #7。

### 4.2 表设计（migration 0010，v2 修订）

```sql
CREATE TABLE chip_distribution (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    trade_date          TEXT NOT NULL,
    winner_ratio        REAL,            -- 获利比例 [0,1]
    avg_cost            REAL,            -- 平均成本（不复权口径，已折回）
    cost_5              REAL,            -- 90% 成本区间下沿（不复权）
    cost_95             REAL,            -- 90% 成本区间上沿（不复权）
    concentration_90    REAL,            -- 90% 集中度
    estimation_status   TEXT NOT NULL,   -- burn_in / mature / insufficient_data
    turnover_used       REAL,            -- 当日换手率快照（审计：结果对得上输入）
    amount_used         REAL,            -- 当日成交额快照（评审 #6：对照核 vwap 峰的
                                         --  输入审计；close 峰下仅留痕）
    source              TEXT NOT NULL,   -- 'self_computed'
    params_json         TEXT NOT NULL,   -- {A, k_cap, peak_mode, dist_shape, n_bins,
                                         --  burn_in_days, price_pad, window}
    run_id              TEXT NOT NULL,   -- --all 时全局单 run_id
    rule_version        TEXT NOT NULL,   -- 'chip_v1_close_tri'（形状/峰值编码进版本串，
                                         --  评审 #5：改核不改参也可分辨）
    config_hash         TEXT NOT NULL,
    computed_at         TEXT NOT NULL,
    UNIQUE(symbol, trade_date)
);
```

- 存四项指标**不存网格**（2000×750 网格全池落库 ≈ 5100 万行，无消费方，不值得；
  需要时从 daily_bars 确定性重算即可——"派生可重建"原则）
- `turnover_used` + `amount_used` 双快照：换核形状（close→vwap）或数据回填修正后，
  结果与输入仍可对账
- `params_json` 逐行固化 + `rule_version` 编码核形状 + `config_hash` 三重可审计

### 4.3 参数（config/indicators.yaml 新增 chip 段，v2 修订）

```yaml
  chip:                        # ⚠️ 筹码分布模型参数为第一版默认值，待东财恢复后
                               #    交叉验证（§6）再定；人工核对期纪律同 §5.2
    decay_factor: 0.7          # A：换手强度系数（评审 #2：1.0 高估新筹码，0.6–0.7 行业
                               #    经验区间；待验证，不默认正确）
    turnover_cap: 0.8          # k_cap：异常数据护栏（A=0.7 下仅 turnover≥114% 触发）
    peak_mode: close           # close | vwap（评审 #4：close 峰默认，vwap 对照保留）
    dist_shape: triangular     # triangular | uniform（对照保留；尖峰族暂缓）
    n_bins: 2000               # 复权域价格网格数
    burn_in_days: 90           # 初始化影响期打标（评审 #3：90 日残差 ~28%@A=0.7）
    price_pad: 0.1             # 网格上下界外扩 10%
```

## 5. 测试计划（v2 增补 4 组）

1. **Golden tests**（锁定公式，同 test_indicators 纪律）：构造 3–5 日人工可算小样本：
   - 两日样例手算 winner_ratio/avg_cost 与实现逐位比对
   - 一字板（low==high）点分布退化
   - 涨停收盘日（close==high）：质量集中在板价附近（close 峰直角三角形）
   - turnover=0 日分布不变；turnover 缺失日跳过；停牌日连续性
2. **性质测试**（不变量）：
   - winner_ratio ∈ [0,1]；Σw 恒为 1（归一化不漂移）
   - avg_cost ∈ [历史 low_adj×0.9, high_adj×1.1]
   - **折回反向验证**（评审 #9）：`avg_cost_raw × factor_d ≈ avg_cost_adj`（1e−9 相对容差）
   - 分位数单调：cost_5 ≤ cost_95
3. **除权连续性**（评审 #7-1，§2.3 核心卖点的守卫）：
   - 构造送股除权样例：断言复权域 winner_ratio 跨除权日连续、avg_cost_adj 按因子
     比例自然衔接（不复权域必然跳变——该跳变只允许出现在折回输出的除权日）
4. **衰减与截断**（评审 #7-2）：
   - turnover=0.9、A=0.7：k_d=0.63，**不触发 cap**（护栏不误伤常规高换手）
   - turnover=2.0（异常）：k_d=0.8（cap 拦截），旧筹码不清零
5. **次新股首日**（评审 #7-3）：
   - 窗口首日=上市首日样例：d₀ 起全部 `burn_in`，第 91 日起 `mature`，无 ok/空档
6. **回归锚**：珀莱雅 603605 全窗口跑一遍，冻结 mature 段首日四项指标值进 golden
   （防未来无意识改动模型行为）。

## 6. 交叉验证计划（东财恢复后）

- 东财 `stock_cyq_em` 一旦网络恢复：抽样 5–10 只、各取近 250 日，比对 winner_ratio
  与 avg_cost 序列。
- **只做秩相关（Spearman ρ）与带宽容差，不做逐点相等**：双方模型参数不同，逐点一致
  既不可能也不必要；ρ ≥ 0.8 且 avg_cost 相对偏差中位 ≤ 5% 视为"同族模型互相印证"。
- **A 与 k_cap 作为可调参数进入验证**（评审 #3）：不预设当前默认正确——若 ρ 不达标，
  先在 {0.6, 0.7, 1.0} × cap {0.8, 0.9} 网格上试参，选 ρ 最高组合回写 config 并记录。
- **已知偏差源清单**（验证时排除后再归因模型）：
  ① 现金分红复权偏差（§2.3，高分红股系统性低估，预期对航空/煤炭影响 > 成长股）；
  ② **turnover 分母口径**（评审 #8）：sina turnover 以流通股本为分母，而模型筹码域
  与 price_adj_factor 涉及总股本口径，未全流通股票（次新/部分国企）存在分母错位；
  ③ burn_in 残留（§3）；
  ④ 东财自身参数未知（它不是基准真值，只是同族参照）。
- 验证结果记执行日志；不通过则回审参数（burn_in/A/形状），不静默调参。

## 7. 明确不做（边界，v2 增补）

1. 不进信号链：不产生 signal_facts、不进 daily.py 默认链、不进排期卡触发/证伪判断；
2. 不做"主力成本/套牢盘"这类叙事性推导标签（LLM 底稿引用时须带估算标注与状态）；
3. 不承诺与东财/通达信逐点一致（模型族相同、参数不同）；
4. 不做逐笔/tick 级精化（无数据源，日频衰减模型是当前数据条件下的诚实选择）；
5. **不还原真实股东成本**（评审 #1）：现金分红在模型中表现为历史成本平移（前复权
   口径固有属性），v1 不区分现金分红与送转除权、不做税务/资金占用口径修正——
   产出的是"前复权口径下的模型估算值"，不是股东真实成本统计。

## 8. 开放问题（v2 修订：已决 5 项，保留 2 项）

**已随评审关闭**（决策记入正文）：核形状默认（close 峰三角，§2.2）、A=0.7（§2.1）、
burn_in=90（§3）、rule_version 编码形状（§4.2）、表加 amount_used（§4.2）。

**保留开放**：

1. **尖峰形状族**（log-normal/beta 核）：v1 不做；若 §6 交叉验证显示 ρ 系统性偏低
   且方向与"日内成交集中度"相关，再立项评估（需引入额外形状参数，代价是可测性下降）。
2. **网格外价格的 status 打标**（v1 评审问题 6）：当前用"余量 10% + 超界 1% 全股重建"
   兜底（§2.5），不单独打标；若实跑出现重建频繁的股票（历史大涨大跌），再考虑加
   `grid_rebuilt` 标注。实施后用全池数据回看一次重建频率，>5% 股数触发则回补设计。

## 9. 实施切分（v2 修订：7 步）

| # | 任务 | 交付 |
|---|---|---|
| 1 | migration 0010 + db checklist | 表 + 测试 |
| 2 | config chip 段（A=0.7/peak=close/burn_in=90） | 参数 |
| 3 | chip_distribution.py 纯函数 + CLI（--all 单 run_id） | 模型 |
| 4 | golden/性质/除权连续性/截断/次新/回归锚测试 | 测试 |
| 5 | adjust.py AdjustResult.notes 加 chip 重算提示（评审 #8，仅提示不耦合） | 联动保险 |
| 6 | 全池重算 + 抽查（手工计算 3 日对照 + 折回反验全量跑） | 数据 |
| 7 | 文档：database_schema/system_design/execution_log/handoff | 留痕 |

预估规模：~300 行实现 + ~250 行测试，一次会话可完成。
