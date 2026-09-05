# 模拟盘（信号决策力实验）设计方案 v2

> 状态：**v2 已按外部评审修订**（v1 结论：设计扎实、conditional approve；三类意见：
> 阻断 B1–B3 / 重要 I1–I4 / 非阻断 3 处，修订映射见 §0.1）
> 日期：2026-09-05（v1 同日，v2 评审修订）
> 关联：`executions`（不可变实际执行台账）、`signal_facts`（决策点全记录）、
> `scripts/signals/daily_watch.py`（状态机语义依据）、`backtest/run_event`（边界参照）
> 定位：**个人判断力实验工具**——与实际买卖完全隔离，不进信号链、不影响任何现有统计

---

## 0. 一句话

在每个信号触发点人工录一行"跟 / 不跟 / 看反"决策（系统自动取收盘价成交、冻结信号
快照），开平仓对称成环；统计三组结果并与"信号即全跟"的机械基线对照——**判断力 =
主观组合收益 − 机械基线收益**，用数据回答"我的偏离到底有没有增值"。

### 0.1 v1 → v2 修订映射（评审意见对照）

| # | 评审意见 | v2 处置 | 位置 |
|---|---|---|---|
| B1 | falsification 事件字段用错（breached_today 含 watch 态，2 日窗口会产双点） | entry/exit 一律用**确认日** `state='active' AND triggered=1`（run==confirm_days）；`breached_today` 非事件字段 | §2.1 |
| B2 | 复权因子版本一致性：entry 冻结因子 + exit 现算因子 origin 不一致，重算扭曲收益 | entry_adj/exit_adj **不落库**；结算时按结算时点库内因子对两日现取 close_raw×factor；"因子版本变化会重算收益"列为已知偏差声明 | §3.2/§3.3 |
| B3 | 停牌/缺价未定义 | 新增停牌处理小节：信号日必有 bar（信号前提）；结算/timeout 顺延至下一有 bar 交易日；hold_days 按交易日历含停牌日 | §2.4/§4 |
| I1 | 价格应同 executions 定点纪律 | close_used/entry_close/exit_close 改 **TEXT 定点**；ret/pnl/因子列 REAL | §3.1/§3.2 |
| I2 | tier_triggered "out→in"不精确（pending_signals 已算在区） | entry 决策点 = 当日 state='triggered' 且**前一交易日 state≠'triggered'**（前日状态扫描，§2.1 明确） | §2.1 |
| I3 | box_entry 去抖语义歧义 + sell_zone/breached 不该当 buy 信号 | 去抖精确定义"决策点产生后 5 个交易日冷却期"；仅 buy_zone 转变计 entry；去抖期留 note 供"无效箱体"分层 | §2.1/§5 |
| I4 | §2.5 跨文档引用歧义 | 改为"system_design §2.5"显式外部引用 | §5 |
| 四.1 | 并发无上限 + 一股一仓被迫 skip 污染 skip 组 | 同意不设并发上限；**结构性 skip 打标** `constraint='single_position'`，统计与自主 skip 分层 | §3.1/§5 |
| 四.2 | 决策窗口 T+1 收盘确认；报告默认含 late 并标注数 | 采纳；统计恒备"剔除 late"对照 | §5/§6 |
| 四.3 | box 去抖参数保留，语义先定 | 去抖定义见 §2.1；参数 ⚠️ 待核对 | §2.1 |
| 四.4 | exit 信号集加"档位深度脱离" | 新增 `deep_exit`：收盘 < entry 档下沿×(1−5%)（非档位入场退 entry_close×(1−5%)），生成 exit 决策点（主观 follow/skip） | §2.2/§4 |
| 四.5 | skip 基线语义确认 | 确认：基线完全机械化，与主观 skip 无关 | §5 |
| 四.6 | late 默认含+标注 | 采纳 | §6 |
| 四.7 | 命名无冲突；与 backtest 边界写入 docstring | 采纳：scripts/paper/ 模块 docstring 声明不复用 run_event | §7 |
| 小1–3 | 确认日字段/偏差声明显式化/停牌行 | 已并入 B1/B2/B3 修订 | §2.1/§3.3/§4 |

## 1. 背景与目标

1. 系统现状：`executions` 记实际买卖（append-only + 信号快照冻结），但没有"当时我
   怎么想"的记录——实际执行混入了仓位约束、资金约束、情绪，无法单独评估判断力。
2. 目标：把"判断"从"执行"里剥离。每笔模拟单固定名义仓位，唯一变量是决策本身
   （方向与时机），统计上隔离仓位管理干扰。
3. 对照框架（本设计的灵魂）：
   - **主观组合** = 所有 follow 决策构成的模拟仓位集合的实际盈亏；
   - **机械基线** = 同一决策点集合"信号即全跟"的朴素机械化组合（同名义、同进出规则，
     完全机械化、与主观行为无关）；
   - **判断力差值 = 主观组合 − 机械基线**。skip 掉亏损信号、counter 看反正确 → 差值
     为正；skip 掉盈利信号 → 差值为负。
   - `backtest/run_event` 是"排期卡择时层"机械化（衰竭锚口径，akquant 路径），覆盖
     信号集不同；本设计的机械基线自算确定性实现，**不依赖 backtest 包**（隔离纪律）。
4. 与 `executions` 的关系：**完全隔离**。真实单继续走 executions；模拟决策永不写
   executions，真实执行也不自动生成模拟决策。

## 2. 核心概念

### 2.1 决策点集合（entry 类，可枚举）

**状态型信号事件化（关键规则，v1 评审抓漏后确立）**：只有**状态转变日**产生决策点，
连续同状态期间不重复产生。

**字段语义以 `scripts/signals/daily_watch.py` 实际实现为准**：

| 决策点 | 事件精确定义（v2 修订） |
|---|---|
| 档位触发 | 当日 `tier_triggered.state='triggered'`（triggered=1，进入价区且同锚衰竭≥2）**且前一交易日 state≠'triggered'**（前日状态扫描；注意 `pending_signals` 已算"在区"——v1 的"out→in"表述不准，v2 改为"→triggered 转变"） |
| 右侧确认 | `right_side`，state='confirmed'（事件型，天然唯一） |
| 证伪线跌破 | **确认日**：`falsification_breach.state='active' AND triggered=1`（即连续跌破日数 run==confirm_days 的当日）。⚠️ `breached_today=true` 在 watch 态（run<confirm_days）即为真，**不是事件字段**，不得用于枚举——否则 2 日确认窗口会产出两个决策点且与"一点一决"唯一键冲突。entry 与 exit 用同一确认日口径 |
| 箱体进入买区 | `box_position`：前一交易日 state 非 buy_zone 且当日 state='buy_zone' 的转变日，**且距上一 box_entry 决策点 > 5 个交易日**（冷却期去抖，⚠️ 参数）。`sell_zone`/`box_breached` 同样 triggered=1 但**不是** buy 决策点 |

衰竭信号 ≥2 首日（exhaustion）缓办（v2 再议）。

**open 期间同股再触发 entry 决策点**：一股一仓（§7.6）下仅允许 `skip` 且自动打标
`constraint='single_position'`（结构性 skip，统计与自主 skip 分层，评审四.1），系统
拒绝 follow/counter。

**前日状态扫描的实现口径**：决策点枚举 = 按 symbol 扫 `signal_facts` 日期序，取相邻
两行比较 state；`signal_facts` 缺行日（停牌/管线未跑）视为状态延续前值（§2.4）。

### 2.2 决策三态与对称退出

- 每个决策点人工录一行：`follow`（跟）/ `skip`（不跟）/ `counter`（看反）。
- **对称退出**：follow 建仓后，该股出现 **exit 类信号** → 产生"平仓决策点"：
  `follow` = 平仓结算、`skip` = 继续持有。exit 信号集（v2 增补第 3 项）：
  1. 证伪线跌破确认日（§2.1 同口径，falsification_breach triggered=1）；
  2. 右侧 `stopped_out`；
  3. **档位深度脱离**（v2 新增，评审四.4）：收盘 < 深度脱离线——
     `deep_exit_line = 档位入场取触发档 zone_low×(1−deep_exit_pct)；非档位入场取
     entry_close×(1−deep_exit_pct)`，`deep_exit_pct` 默认 0.05（⚠️ 待核对）。
     不加此项则无退出信号的仓位会被 timeout 静默吸收，稀释判断力信号。
- **兜底**：持有满 `max_hold_days=60` 交易日系统强制按收盘结算（`timeout`，不问人，
  不产生决策行）。
- `counter`（仅 entry 类）：不开仓（A 股无裸卖空），评价用反向虚拟收益，单列统计。
- `manual` 平仓：随时可人工平仓（reason 必填），单列统计。

### 2.3 反作弊五防线（判断力实验的命门）

1. **价格不可自填**：entry/exit 成交价一律系统按决策日收盘自动取（用户只输入三态
   与备注），杜绝事后挑价。
2. **录入窗口**：决策须在触发日 T 盘后 → T+1 收盘前录入；超窗标 `late=true`。
3. **决策不可改**：append-only；录错走冲正行（同 executions.reverses 模式）。
4. **快照冻结**：决策行冻结当日 signal_facts 详情与收盘价；**复权因子版本变化会
   重算 open 仓位收益**——列为已知偏差声明（见 §3.3，同 chip 设计 v2 §2.3 写法）。
5. **漏录可见**：报告段列"今日触发未录"清单；漏录不强制补，但可见即是约束。

### 2.4 停牌处理（v2 新增，评审 B3）

- **信号日必有 bar**：信号以当日行情为计算前提，停牌日无 bar 即无 signal_facts 行，
  天然不产生决策点。
- **录入日不受停牌影响**：录入窗口校验的是日期差（T→T+1），T 日收盘价在决策行冻结
  （T 日有 bar 才有信号）。
- **结算/timeout 顺延**：exit 结算或 timeout 到期日遇停牌（无 bar、无 close）→
  **顺延至下一有 bar 交易日**，以其收盘结算；exit 信号在停牌日不可能触发（无信号行）。
- **hold_days 按交易日历计（含停牌日）**：停牌占用持有期——timeout 口径的诚实选择
  （停牌风险由持仓者承担）；若未来想改为"剔除停牌日"，另议。
- 长期停牌（如神华 2025-08 重组 10 日）下 exit 信号缺失 → 自然由 timeout 兜底。

## 3. 数据模型（migration 0011，两张表）

### 3.1 `paper_decisions` — 决策流水（append-only，不可更新）

```sql
CREATE TABLE paper_decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol               TEXT NOT NULL,
    decision_date        TEXT NOT NULL,   -- 决策日 T（=信号事件日）
    decision_type        TEXT NOT NULL,   -- entry / exit
    signal_source        TEXT NOT NULL,   -- tier_triggered / right_side /
                                          -- falsification_breach / box_entry /
                                          -- deep_exit / timeout / manual
    signal_snapshot_json TEXT NOT NULL,   -- 冻结：signal/state/details/close_raw/
                                          -- price_adj_factor（决策语境）
    decision             TEXT NOT NULL,   -- follow / skip / counter
    close_used           TEXT,            -- T 日收盘（不复权，定点 TEXT，系统取；
                                          --  同 executions 价格纪律，I1）
    quantity             INTEGER,         -- follow-entry：notional/close 取整百股
    notional             REAL,            -- 固定名义（config）
    constraint_tag       TEXT,            -- 'single_position'（结构性 skip 打标，
                                          --  评审四.1）否则 NULL
    late                 INTEGER NOT NULL DEFAULT 0,  -- 超 T+1 窗口
    reversed_by          INTEGER,         -- 冲正行 id
    note                 TEXT,
    run_id               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE(symbol, decision_date, decision_type, signal_source)  -- 一点一决
);
```

### 3.2 `paper_positions` — 虚拟仓位（状态机，可更新）

```sql
CREATE TABLE paper_positions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol             TEXT NOT NULL,
    entry_decision_id  INTEGER NOT NULL REFERENCES paper_decisions(id),
    entry_date         TEXT NOT NULL,
    entry_close        TEXT NOT NULL,   -- 不复权定点（I1）
    quantity           INTEGER NOT NULL,
    notional           REAL NOT NULL,
    deep_exit_line     TEXT NOT NULL,   -- 深度脱离线（entry 时按 §2.2 冻结，TEXT）
    status             TEXT NOT NULL,   -- open / closed
    exit_decision_id   INTEGER REFERENCES paper_decisions(id),
    exit_date          TEXT,
    exit_close         TEXT,            -- 不复权定点（I1）
    exit_source        TEXT,            -- falsification / stopped_out / deep_exit /
                                        --  timeout / manual / reversal
    hold_days          INTEGER,         -- 交易日历日数（含停牌，§2.4）
    ret                REAL,            -- 结算时按库内现值因子计算（见 §3.3）
    pnl                REAL,            -- notional × ret
    closed_at          TEXT
);
```

**v2 关键修订（评审 B2）**：`entry_adj`/`exit_adj` **不落库**。结算时按**结算时点
daily_bars 库内 price_adj_factor** 对 entry/exit 两日现取 `close_raw × factor` 计算
`ret`——两日因子 origin 天然一致（同一版本），不存在 v1"entry 冻结旧因子 + exit
用新因子"的错配。`signal_snapshot_json` 里冻结决策日因子仅作审计。

### 3.3 已知偏差声明（评审 B2/I1，同 chip v2 §2.3 写法）

- **因子版本变化会重算 open 仓位收益**：adjust.py 全量重建因子后（晚到的分红/送转），
  已结算仓位的 ret 若按新因子重算会变化；v1 约定**结算即定格**（已结算行不回改），
  open 仓位结算时自然采用最新因子。此偏差与前复权口径全局一致，不单独补偿。
- **复权口径平移历史成本**：现金分红在前复权口径下平移历史成本（系统全局口径），
  模拟盘 ret 含此效应，不还原真实股东成本。
- 价格列定点 TEXT（同 executions 纪律）；ret/pnl REAL 为展示级精度（非账务系统）。

### 3.4 配置（config/paper.yaml，新文件）

```yaml
notional_per_trade: 100000   # 每笔固定名义（元）
max_hold_days: 60            # 兜底结算持有期（交易日历日，含停牌）
decision_window_days: 1      # 决策录入窗口：T 盘后 → T+1 收盘（超窗 late）
box_entry_debounce_days: 5   # 箱体 buy_zone 决策点冷却期（⚠️ 待核对）
deep_exit_pct: 0.05          # 档位深度脱离线（⚠️ 待核对）
baseline: naive_follow_all   # 机械基线口径（v1 唯一）
```

## 4. 决策-结算流程

```
T 日盘后：daily 报告 → 报告"模拟盘"段列出今日未决决策点（含编号）
   ↓ decide 录入（follow/skip/counter；结构性 skip 自动打标）
   └ follow-entry → open position（deep_exit_line 冻结）
T+n：该股出现 exit 类信号（证伪确认/stopped_out/deep_exit 触发）
   → 报告列"待平仓决策点"
   ↓ follow → 平仓结算（exit_close 系统 T 日收盘）
   └ skip → 继续持有（等下一个 exit 信号）
任意时刻：hold_days ≥ 60 → 顺延至下一有 bar 交易日 timeout 强制结算
随时：manual 平仓（reason 必填，单列统计）
```

- 结算 `ret = (exit_close×f_exit)/(entry_close×f_entry) − 1`，两因子取**结算时点**
  库内值（§3.2/§3.3）；`pnl = notional × ret`；hold_days 按 trading_calendar
  （§2.4 含停牌）。
- **同日多个 exit 信号**（如证伪确认与 deep_exit 同日触发）：任一 follow 即平仓
  （exit_source 取实际录入的那条），其余同日 exit 决策点自动失效（decide 拒绝重复
  平仓，标 superseded）。
- timeout 由 settle 扫描自动执行；exit 决策点超窗不录 → position 持续 open 至
  timeout（漏录可见但不强制）。
- 冲正：决策行标 reversed_by；未平仓 position 强制平仓结算（exit_source='reversal'）。

## 5. 统计口径（判断力评分）

| 组 | 口径 | 含义 |
|---|---|---|
| follow 组 | positions 全量：笔数/胜率/平均 ret/累计 pnl | 实际跟单的结果 |
| skip 组 | 每条 skip 的虚拟跟入收益（决策点→该信号基线退出日） | 正=错过盈利 / 负=躲过亏损；**分层：自主 skip vs 结构性 skip**（constraint_tag，评审四.1） |
| counter 组 | −虚拟跟入收益 | 看反的对错率 |
| **机械基线** | 同决策点集全 follow（同进出规则；**退出也全跟**，与该信号是否被 skip 无关——评审四.5 确认） | 严格跟卡会怎样 |
| **判断力差值** | 主观组合累计 pnl − 机械基线累计 pnl | 核心指标 |

分层：signal_source / symbol / decision / constraint_tag / late；box_entry 的 note
支持"主观认为无效箱体"标注分层（评审四.3）。

**late 口径**：报告与统计默认**含 late** 并显著标注"含 N 条 late"；stats 恒备
`--exclude-late` 对照（评审四.2/四.6）。

**样本量纪律**：笔数 < 30 时只输出描述统计并明示"样本不足不下结论"（system_design
§2.5 同款纪律——外部引用，防止三五笔连胜就自认有超额判断力）。

## 6. CLI 与报告接入

```
uv run python -m scripts.paper.decide --pending                 # 列全部待决策点（含编号）
uv run python -m scripts.paper.decide --pick <N> --decision follow|skip|counter [--note]
uv run python -m scripts.paper.settle [--as-of D]               # timeout 强制结算扫描
uv run python -m scripts.paper.reversal <decision_id> --reason "..."
uv run python -m scripts.paper.stats [--symbol S] [--exclude-late]
```

- 每日全池报告末尾新增**"模拟盘"独立段**（不动五级排序）：
  ① 今日触发未录决策点清单（含 decide --pick 编号）；
  ② 待平仓决策点（exit 信号已触发未决）；
  ③ open 仓位浮盈快照；④ 累计统计摘要（follow 胜率/累计 pnl/判断力差值/含 late N 条）。
- 单股报告 §3 决策点旁加一行"模拟盘：已决 follow（+x.x%）/待决"提示。
- settle 与待决清单由 report 生成时顺带计算（纯读），不进 daily 信号链；decision
  录入独立手触发。

## 7. 明确不做（边界）

1. 不做盘中/实时：全部盘后决策、收盘价成交；
2. 不做自动跟单：决策必须人工录入（自动化=没有判断可测）；
3. counter 不开空仓（A 股约束），仅标记与反向虚拟评价；
4. 不与真实资金混算：模拟盘统计永不并入 executions/收益报告；
5. LLM 不参与：决策是人录的，统计是 Python 算的（LLM 边界）；
6. 不做多空双向/杠杆/分批加减仓（v1 单向多头、一笔一仓）；分批留 v2；
7. **与 backtest 的边界**（评审四.7）：`scripts/paper/` 全模块 docstring 声明——
   机械基线为内置朴素实现，不复用/不调用 `scripts/backtest/`（akquant 路径），
   两套对照口径互不影响。

## 8. 评审问题处置与遗留开放问题

**v1 七问已全部处置**（§0.1 映射表四.1–四.7）；**v2 评审三项阻断、四项重要、三项
非阻断全部采纳**（映射见 §0.1）。

**遗留开放问题（实施中带默认值，运行后回看）**：

1. `box_entry_debounce_days=5` / `deep_exit_pct=5%` 两个新参数的实际触发频率——
   首月运行后按全池数据回看，过密/过疏则调参（⚠️ 人工核对期纪律）；
2. 结构性 skip 的量级——若"一股一仓"导致结构性 skip 占比 >30%，说明决策点密度
   与单仓规则冲突，考虑 v2 放开同股第二笔（独立名义）或缩短 max_hold_days；
3. timeout 结算占比——若 >40% 仓位拖到 timeout，说明 exit 信号集仍不充分，
   回审 deep_exit_pct 或增加"持有期动量反转"退出。

## 9. 实施切分（评审通过后）

| # | 任务 | 交付 |
|---|---|---|
| 1 | migration 0011（两表）+ db checklist | 表 |
| 2 | config/paper.yaml + 加载 | 参数 |
| 3 | scripts/paper/decide.py（决策点枚举：前日状态扫描/转变事件化/去抖/窗口校验/结构性 skip 打标/录入） | 录入 |
| 4 | scripts/paper/settle.py（exit 信号扫描/timeout 顺延/manual/reversal/结算计算） | 结算 |
| 5 | scripts/paper/stats.py（三组 + 机械基线 + 差值 + late 双口径 + 分层） | 统计 |
| 6 | report.py 接入模拟盘段（全池日报 + 单股提示行；纯读） | 报告 |
| 7 | tests（事件枚举 golden（含 falsification 确认日/前日状态扫描）/窗口与 late/停牌顺延/结算状态机/因子现取/基线对照/冲正/结构性 skip/报告段） | 测试 |
| 8 | 文档：database_schema/system_design/execution_log/handoff | 留痕 |

预估规模：~900 行实现 + ~500 行测试，1–2 次会话。
