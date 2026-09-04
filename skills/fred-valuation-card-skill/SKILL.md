---
name: fred-valuation-card-skill
description: 为个股（以 A 股为主）生成「估值排期卡」——一套面向左侧交易者的分批买入作战卡，包含盈利底稿、历史底部估值刻度、估值体系切换判断、EPS×PE 情景矩阵、四源胜率打分卡（估值位置/盈利轨迹/衰竭信号/体系稳定性，输出区间并进 Kelly 定仓位上限）、三档买入排期、衰竭信号打分卡、波段仓/右侧确认仓规则、证伪线与锚维护日历。当用户要求分析某只股票"从哪开始买、怎么分批、什么时候不许动"、评估一笔买入的胜率、做抄底计划、估值排期、左侧交易计划，或要求把估值锚与衰竭信号框架应用到新标的时使用。也适用于持仓股的季度锚复核。不用于荐股、短线择时信号、右侧趋势跟踪系统本身，或纯基本面研报。
---

<!--
Copyright © 2026 Fred Chen. All rights reserved.
Project: fred-valuation-card-skill
Created by: Fred Chen
Date: 2026-07-19

-->

# 估值排期卡 Skill

为左侧交易者生成个股估值排期卡。核心思想：**估值锚定档（赔率管理），胜率打分（仓位大小），衰竭信号择时（时机确认），证伪线兜底（认错机制）**。第一档允许买早，第二、三档必须等衰竭信号，三档买完后停止补仓。仓位递增的理由是证据变多，不是跌幅变大。

## 工作流程

按顺序执行，不要跳步。每一步的产物是下一步的输入。

### 0. 排期卡复用检查

先查该股是否已有排期卡（`uv run python -m scripts.pipeline.card list <symbol>`，或 Web UI /cards）：

- **已有卡且锚未过期**（未跨过新财报披露、证伪线未触发、未到 next_review）→ **复用旧卡**，在产出中注明复用日期，不重跑全流程；但复用前必须刷新四项：
  1. 现价落位（现价相对三档价区/箱体的位置）；
  2. 衰竭信号状态（重跑底稿 `signal_status` 并人工逐条核对）；
  3. 波段仓箱体与右侧关键位是否仍然有效（价格结构已破坏原箱体 → 重画或宣布不适用）；
  4. 旧卡锚维护日历上的到期事项（如"盈利改善→档线上移"已到复核点的，先执行档线调整——走新 draft + 人工激活，§5.6）。
- **锚已过期**（跨过财报披露 / 证伪线触发 / next_review 已到未复核）→ 禁止复用，走完整流程重建。**不允许用过期锚出结论**（纪律同第 8 步）。

### 1. 取数

**主输入 = 系统底稿**。先运行底稿导出器，从本地 `data/market.db` 取全部存档事实（Token 节约：只消费存档的事实与底稿，不重新抓数、不自己算指标）：

```bash
uv run python -m scripts.pipeline.card_inputs <symbol>
# → cards/{symbol}/inputs_{YYYY-MM-DD}.json，十段：meta（含各来源数据截止日期）/
#   盈利底稿（序列+TTM）/一致预期（FY1–FY3+裂口对照）/行业因子快照（factor_snapshot）/
#   估值刻度候选（恐慌低点+PE 分位数）/
#   现价与股本/衰竭信号具体化参数/当前信号状态/日频监测摘要/参数回声
```

底稿覆盖不到的数据（缺口）才调 kimi-datasource 插件补取；数据源不可用时说明缺口，不要编造数字。接口名对照（旧 iFinD 写法 → 现插件）：

- `ifind_get_financial_statements` → `stock_finance_data_get_financial_statements`（利润表：营收/归母净利/EPS）
- `ifind_get_price` → `stock_finance_data_get_price`（日线行情；周线由系统聚合，见下）
- `ifind_get_forecast` → `stock_finance_data_get_forecast`（分析师一致预期 FY1–FY3）

行情口径：系统库存**不复权价 + 复权因子**，周线由系统从逐日复权日线聚合。卡片价区、前低、现价、箱体一律用**不复权**口径；复权价仅供技术比较（找恐慌低点、背离），不进卡片数字。

### 2. 盈利底稿

- 计算 TTM 归母净利与 TTM EPS（最近年报 − 去年同期季报 + 最近季报）。
- 列出盈利轨迹表（营收/净利同比），判断盈利处于：增长 / 停滞 / 下滑 / 恶化。
- **一致预期裂口检查**：对比券商 FY1 增速预期与最近季报实际增速。若预期显著高于实际趋势，在卡中标注裂口，情景设定以实际趋势为准，不采用券商数字。
- **行业因子快照**：若底稿含 `factor_snapshot` 段（schema ≥ card_inputs_v2），写出该股相关行业因子的当前水平、数据日期与近 20/60 日方向（`status` 为 stale 的因子只标注"读数过期"，不得当作现状引用）。该段是第 4 步情景假设的事实底座。底稿无此段或该股无因子映射时，在卡中标注缺口，禁止编造因子读数。

### 3. 历史底部估值刻度

- 从周线找 3–5 个恐慌低点，计算每个低点当时的 PE（用当时 TTM EPS）。底稿 `valuation_scale.panic_lows` 已给出系统识别的恐慌低点（含当日 PE(TTM) 与分位数），优先采用并核对。
- **硬性规则（§3.2）：PE 刻度必须标注 3 年样本区间**——格式"刻度基于 YYYY-MM-DD 至 YYYY-MM-DD 的 N 个恐慌低点"，直接取底稿 `valuation_scale.sample_window`。引用样本区间之外的更早历史估值时，必须显式声明为**样本外**证据，禁止当作同体系刻度使用。
- 观察刻度序列的方向：刻度逐轮下移 = **估值体系切换**（如成长股向价值股迁移），锚必须设在新体系内，禁止引用旧体系的高估值历史作为"便宜"依据；刻度稳定 = 可复用历史底部刻度做锚。
- 按生意类型选锚（稳定消费→PE 带；保险→P/EV+股息率；银行→PB+股息率；强周期→PB；高增长→情景 EPS×远期 PE）。细则见 [references/valuation-anchors.md](references/valuation-anchors.md)。

### 4. 情景矩阵与三档排期

- 设 EPS 三情景（中性/悲观/极悲），以 TTM 和最近季报实际趋势为底子，不拍脑袋。
- **因子假设**：对周期/成本敏感型公司（底稿 `factor_snapshot` 有映射的），EPS 情景必须显式挂行业因子假设——每个情景注明关键因子的假设水平（如"中性 = 布伦特 85 美元/桶"），并写入卡片 JSON 的 `earnings.factor_assumptions`（注意：draft 文件根字段是 `earnings`，入库时映射为 `earnings_scenarios_json` 列；根级 `earnings_scenarios` 会被 schema additionalProperties 拒绝）。条目格式 `{"code","name","unit","level","as_of_date","note"}`，`level` 为十进制字符串，原生单位。因子→EPS 的弹性推导写在卡的"因子假设"小节备查。**假设水平以 `card_inputs_v2` 底稿 `factor_snapshot` 段的当期读数（`close` + `trade_date`）为锚**；skill 不自行从外部数据源（kimi-datasource、网页）取当前读数；若底稿因子 `status` 为 `stale` 或 `missing`，在卡中如实标注缺口，不得编造。无因子映射的公司（稳定消费类等）省略该字段并在卡中说明理由。
- 设 PE 三情景（乐观/中性/悲观），取自第 3 步判定的**当前估值体系**。
- 运行 `scripts/build_schedule.py` 生成矩阵和三档排期表：

```bash
python3 scripts/build_schedule.py --eps 3.9,3.5,3.2 --pe 18,15,12.5 --price 56.70
```

- 排期结构：第一档（30%，估值到位即可买，允许买早）/ 第二档（35%，估值到位 + 衰竭信号 ≥2 项）/ 第三档（35%，同条件）。用户另有偏好时按比例调整。完成第 6 步胜率打分后，可把各档胜率下沿传给 `--winrate` 让脚本按 quarter-Kelly 输出仓位上限，实际档位比例 = min(固定比例, Kelly 上限)。
- 证伪线：三档买完后停止补仓；收盘价有效跌破证伪线 = 极悲情景被击穿，冻结仓位并重做基本面判断，**禁止继续摊低成本**。

### 5. 衰竭信号规则

读 [references/exhaustion-signals.md](references/exhaustion-signals.md)，把三种形态（恐慌型/干涸型/背离型）转成该股票的具体量化阈值（均量基数、前低位置），写入打分卡。底稿 `exhaustion_params` 已按 `config/signals.yaml` 算出实数（下跌起点后前 4 周均量基数、2 倍放量阈值、40–60% 缩量阈值、不复权前低），直接采用并核对；`signal_status` 给出当前各信号 active 状态与活跃计数。规则只有一条：信号 ≥2 项才释放第二、三档；不足时价格到了也不动。

### 6. 胜率打分

读 [references/win-rate-scorecard.md](references/win-rate-scorecard.md)，按四源合成各档胜率区间：

- 先锁定持有期（左侧仓 12–24 个月），再把现价代入矩阵写明"市场定价组合"，定基准胜率；
- 盈利轨迹、衰竭信号、体系稳定性逐项加减，每项挂证伪点；基准 + 加减，禁止乘法；
- 输出三档各自的胜率区间（第一档不含信号加分；第二、三档按信号 ≥2 项已确认计），区间宽度 >15 个百分点 = 证据不足，该档按固定比例下限执行；
- 各档胜率下沿传给 `scripts/build_schedule.py --winrate` 计算 quarter-Kelly 仓位上限。

### 7. 波段仓与右侧确认仓规则

- 波段仓：从日线识别当前箱体（若处于震荡结构），写下沿买入区、上沿卖出区、证伪线（收盘有效跌破下沿无条件退出）、固定仓位上限。与左侧仓分开记账。
- 右侧确认仓：写触发条件（放量突破关键位 + 回踩不破）、止损位、独立记账声明。触发前这笔钱不动。**止损位即卡内 `right_side_trigger.stop_level`，是右侧仓唯一止损口径**（状态机 confirmed 后按它逐日跟踪落行，收盘跌破即触发止损决策点进日报 P2）；禁止在卡内或正文另设第二止损线（2026-08-30 恒力复盘教训：双止损口径并存导致执行时无所适从）。
- 若股票当前不在震荡结构，明确写"波段仓当前不适用"。
- **触发后状态机**：触发条件满足后系统进入**持仓跟踪状态（holding）**，每日收盘写入跟踪行（`signal_facts` right_side，triggered=0），记录止损位与现价距止损距离。跟踪期间收盘价 ≤ `stop_level` 即触发 `stopped_out` 并回 `idle`；无 `stop_level` 的卡片 confirmed 后直接回 `idle`（§2.5：无线不猜）。holding 期日报 P2/P3/P4 均有输出（"右侧持仓跟踪"决策点），不会静默。

### 8. 锚维护日历

- 写下一次财报披露窗口与复核规则：盈利改善 → 档线上移；连续两季下滑 → 未买档线下移；出现毛利率异常下滑/费用失控/现金流背离净利 → 立即复核。
- **因子偏离复核**：若卡片带 `factor_assumptions`，写下"关键行业因子偏离建卡假设 ±阈值（阈值见 config/macro_factors.yaml 各因子 `alert_threshold_pct`）→ 锚复核提醒"触发器。偏离提醒只提示复核，不自动改卡；复核后确需调整走新 draft + 人工激活（§5.6）。
- 强调纪律：**锚过期必须下移，不允许用过期锚继续补仓**（这是"抄早"在估值层面的根源）。

### 9. 输出排期卡（双产物）

1. **排期卡 Markdown**：以 [assets/card-template.md](assets/card-template.md) 为骨架，文件名用「{股票名}估值排期卡.md」。所有数据标注来源与截止日期。
2. **卡片 JSON**：严格符合系统第一版 schema 约定（见 `scripts/signals/cards.py` docstring 与 `scripts/pipeline/card.py` CARD_INPUT_SCHEMA）。各段字段一览：

| 卡片 JSON 段（draft 根字段 → 入库列） | 必填字段 | 说明 |
|---|---|---|
| `earnings` → `earnings_scenarios_json` | `eps: {bear, base, bull}` | EPS 三情景（bear/base/bull 全必填，定点十进制字符串） |
| | `factor_assumptions?: [{code, name, unit, level, as_of_date, note}]` | 因子假设，可选，与 `eps` 同级放 `earnings` 内；原样存档不参与机械换算 |
| `valuation` → `valuation_scenarios_json` | `pe` 与 `sample_window` | PE 三情景 + **样本区间标注（§3.2）都在本段**——`sample_window` 不是 `earnings` 的字段 |
| `price_tiers` → `price_tiers_json` | `tiers: [{tier, zone_low, zone_high}]` | 三档价位 |
| `invalidation` → `invalidation_json` | `line` | 证伪线（note 可选） |
| `swing_box` → `swing_box_json` | 各边界字段（box_low/box_high/买区/卖区/box_invalidation） | 波段仓；不适用时省略并在卡中说明 |
| `right_side_trigger` → `right_side_trigger_json` | `trigger_level`, `stop_level?` | 右侧触发位/止损位（§7） |

价格类关键决策值一律定点十进制字符串（如 `"55.00"`）。draft 根字段（currency/price_basis/next_review_at/earnings/valuation/price_tiers/invalidation/swing_box/right_side_trigger/input_snapshot）受 `additionalProperties=false` 约束，**因子假设只能放 `earnings` 内，不能放根级**。写盘后由人工执行入库校验：

```bash
uv run python -m scripts.pipeline.card create-draft <symbol> --json <file>
```

**draft-only 原则（§5.6）**：skill 只产 draft，从不自行激活；`activate`/`reject` 必须经人工明确确认后执行，skill 不代做。

**对话回复必须以「行动纲领」开头**（放在任何表格和细节之前）：用大白话把结论翻译成"每笔钱等什么价、做什么"，一到三句话，让不看正文的人也知道自己现在该干什么。规则：

- 用"钱 × 价位 × 动作"的句式落到具体数字，不写"估值合理、建议观望"这类空话；
- 不用术语（排期/衰竭信号/Kelly/证伪线/一档二档），术语只在正文出现；
- 必须说清"中间地带做什么"（通常是一概不动）——真空地带是设计不是漏洞；
- 按当前落位套句式骨架，再结合该股数字改写：
  - 未到位："底仓级别的买点还没到，XX 元以下才轮到谈便宜，现在到那个价位之间的一切波动都和你无关。"
  - 左侧未到位 + 有波段箱体："大仓位等 XX 元以下的便宜，小仓位在 XX–XX 里赚震荡，中间地带一概不动。"
  - 第 N 档可执行："跌到 XX 元，估值和信号两张门票都齐了，按 X% 上限动手，跌破 XX 元认错冻结。"
  - 右侧确认仓待命："反转还没被证明，等放量站稳 XX 元再上，上不去就当这轮反弹没发生过。"
  - 证伪线触发："先别谈买卖，跌破 XX 元说明原来的判断错了，这只股票的钱一分也不动，等重新研究。"

行动纲领之后，正文同步核心结论（保持规则语言）：估值体系判断、情景矩阵、各档胜率区间与 Kelly 仓位上限、三档排期表、当前价格落在哪一档。

## 语气与边界

- 全程用"排期/触发/证伪"的规则语言，不用"预测/看涨/看跌"的预测语言（对话开头的行动纲领除外，按第 9 步用大白话）。
- 每个数字要么来自数据源（标注来源与日期），要么来自用户设定的情景假设（标注为假设）。禁止编造财务数据。
- 排期卡是仓位管理工具，不是买卖建议；结尾提示证伪线与冻结纪律即可，不追加收益承诺。
- 产物一律 draft-only：卡片 JSON 仅入库为 draft，激活生效必须经人工确认（§5.6），skill 不调用 activate/reject。
