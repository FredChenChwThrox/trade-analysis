# 模拟盘（信号决策力实验）设计方案

> 状态：**draft 待评审**（用户指令：交其他 Agent 审核；已过 5 项需求澄清）
> 日期：2026-09-05
> 关联：`executions`（不可变实际执行台账）、`signal_facts`（决策点全记录）、
> `backtest/run_event`（排期卡机械化先例）、卡片胜率/Kelly 体系
> 定位：**个人判断力实验工具**——与实际买卖完全隔离，不进信号链、不影响任何现有统计

---

## 0. 一句话

在每个信号触发点人工录一行"跟 / 不跟 / 看反"决策（系统自动取收盘价成交、冻结信号
快照），开平仓对称成环；统计三组结果并与"信号即全跟"的机械基线对照——**判断力 =
主观组合收益 − 机械基线收益**，用数据回答"我的偏离到底有没有增值"。

## 1. 背景与目标

1. 系统现状：`executions` 记实际买卖（append-only + 信号快照冻结），但没有"当时我
   怎么想"的记录——实际执行混入了仓位约束、资金约束、情绪，无法单独评估判断力。
2. 目标：把"判断"从"执行"里剥离出来。每笔模拟单固定名义仓位，唯一变量是决策本身
   （方向与时机），统计上隔离仓位管理干扰。
3. 对照框架（本设计的灵魂）：
   - **主观组合** = 所有 follow 决策构成的模拟仓位集合的实际盈亏；
   - **机械基线** = 同一决策点集合"信号即全跟"的朴素机械化组合（同名义、同进出规则）；
   - **判断力差值 = 主观组合 − 机械基线**。skip 掉亏损信号、counter 看反正确 → 差值
     为正（判断有增值）；skip 掉盈利信号 → 差值为负。
   - 已有 `backtest/run_event` 是"排期卡择时层"机械化（衰竭锚口径），覆盖信号集不同；
     本设计的机械基线是"同决策点集全跟"的朴素版，自算确定性实现，不依赖 backtest 包
     （隔离纪律），两者对照留二期。
4. 与 `executions` 的关系：**完全隔离**。真实单继续走 executions；模拟决策永不写
   executions，真实执行也不自动生成模拟决策（是否跟卡由人录入）。

## 2. 核心概念

### 2.1 决策点集合（entry 类，可枚举）

**状态型信号事件化（关键规则）**：只有**状态转变日**产生决策点——`out → in` 转变日
录一次，连续在区/在区外期间**不重复**产生（tier_triggered 在连续在区期间每日都
triggered=1，如南航连续四日都有 T1 行，若不事件化会每天重复生成决策点）；离区后再
进区 → 新决策点。箱体 buy_zone 另加 5 日去抖（横盘期反复进出刷决策点，⚠️ 参数）。

信号触发事件（`signal_facts` 已有全部数据），v1 集合：

| 决策点 | signal_facts 事件语义 |
|---|---|
| 档位触发 | `tier_triggered` triggered=1 且该档状态 out→in 转变日 |
| 右侧确认 | `right_side`，state=confirmed（事件型，天然唯一） |
| 证伪线跌破 | `falsification_breach`，breached_today=true（事件型；同时也是 exit 类，见 2.2） |
| 箱体进入买区 | `box_position` 非 buy_zone → buy_zone 转变日（5 日去抖） |

衰竭信号 ≥2 首日（exhaustion）缓办（v2 再议，先控制决策点密度）。

**open 期间同股再触发 entry 决策点**：一股一仓（§7.6）下仅允许 `skip`（可备注），
系统拒绝 follow/counter——避免加仓语义（分批留 v2）。

### 2.2 决策三态与对称退出

- 每个决策点人工录一行：`follow`（跟）/ `skip`（不跟）/ `counter`（看反）。
- **对称退出**：follow 建仓后，该股出现 **exit 类信号**（证伪线跌破 / 右侧
  stopped_out）→ 产生"平仓决策点"：`follow` = 平仓结算、`skip` = 继续持有
  （等下一个退出信号）。**兜底**：持有满 `max_hold_days=60` 交易日系统强制按收盘
  结算（标 `timeout`，不问人）。
- `counter`（仅 entry 类）：不开仓（A 股无裸卖空），标记"我认为该信号是错的"，
  评价用反向虚拟收益（= −跟入收益），单列统计不进主观组合。
- `manual` 平仓：随时可人工平仓（reason 必填），单列统计——区分"信号驱动判断"与
  "情绪驱动操作"，不与信号判断混算。

### 2.3 反作弊五防线（判断力实验的命门）

1. **价格不可自填**：entry/exit 成交价一律系统按决策日收盘自动取（用户只输入三态
   与备注），杜绝事后挑价。
2. **录入窗口**：决策须在触发日 T 盘后 → T+1 收盘前录入；超窗标 `late=true`，
   统计双口径（含 late / 剔除 late）——迟录的"决策"可能已偷看 T+1 行情。
3. **决策不可改**：append-only；录错走冲正行（同 executions.reverses 模式），冲正
   永久可见。
4. **快照冻结**：决策行冻结当日 signal_facts 详情与收盘价，事后信号重算不改变
   历史决策语境。
5. **漏录可见**：报告段列"今日触发未录"清单；漏录不强制补，但可见即是约束。

## 3. 数据模型（migration 0011，两张表）

### 3.1 `paper_decisions` — 决策流水（append-only，不可更新）

```sql
CREATE TABLE paper_decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol               TEXT NOT NULL,
    decision_date        TEXT NOT NULL,   -- 决策日 T（=信号触发日）
    decision_type        TEXT NOT NULL,   -- entry / exit
    signal_source        TEXT NOT NULL,   -- tier_triggered / right_side /
                                          -- falsification_breach / box_entry /
                                          -- timeout / manual
    signal_snapshot_json TEXT NOT NULL,   -- 冻结：signal/state/details/收盘价/因子
    decision             TEXT NOT NULL,   -- follow / skip / counter
    close_used           REAL,            -- T 日收盘（不复权，系统取）
    quantity             INTEGER,         -- follow-entry：notional/close 取整百股
    notional             REAL,            -- 固定名义（config，默认 100000）
    late                 INTEGER NOT NULL DEFAULT 0,  -- 超 T+1 窗口
    reversed_by          INTEGER,         -- 冲正行 id（冲正模式）
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
    entry_close        REAL NOT NULL,   -- 不复权
    entry_adj          REAL NOT NULL,   -- × 当日 factor
    quantity           INTEGER NOT NULL,
    notional           REAL NOT NULL,
    status             TEXT NOT NULL,   -- open / closed
    exit_decision_id   INTEGER REFERENCES paper_decisions(id),
    exit_date          TEXT, 
    exit_close         REAL,
    exit_adj           REAL,
    exit_source        TEXT,            -- falsification / stopped_out / timeout / manual
    hold_days          INTEGER,         -- 交易日数
    ret                REAL,            -- 复权收益率 = exit_adj/entry_adj − 1
    pnl                REAL,            -- notional × ret
    closed_at          TEXT
);
```

- 收益率用**复权口径**（entry_adj/exit_adj），除权日持仓自然处理；pnl 按 notional × ret。
- skip/counter 不生成 position 行；其虚拟评价由统计层现算（决策点→首个退出信号或
  timeout 窗口的复权收益），不落库。
- 冲正：决策行标 reversed_by 后，关联 position 若未平仓则强制平仓结算（标
  `manual`，reason='reversal'）。

### 3.3 配置（config/paper.yaml，新文件）

```yaml
notional_per_trade: 100000   # 每笔固定名义（元）
max_hold_days: 60            # 兜底结算持有期（交易日）
decision_window_days: 1      # 决策录入窗口：T 盘后 → T+1 收盘（超窗 late）
baseline: naive_follow_all   # 机械基线口径（v1 唯一）
```

## 4. 决策-结算流程

```
T 日盘后：daily 报告 → 报告"模拟盘"段列出今日未决决策点
   ↓ 用户录 decide（follow/skip/counter）——follow-entry 生成 open position
T+n：该股出现 exit 类信号 → 报告列"待平仓决策点"
   ↓ follow → 平仓结算（exit_close/exit_adj 系统取 T 日收盘）
   └ skip → 继续持有（等待下一个 exit 信号）
任意时刻：hold_days ≥ 60 → 系统强制 timeout 结算
随时：manual 平仓（reason 必填，单列统计）
```

- 结算收益 `ret = exit_adj/entry_adj − 1`；`pnl = notional × ret`；hold_days 按
  trading_calendar 交易日数。
- timeout 结算是系统行为，不产生决策行（exit_decision_id=NULL，exit_source='timeout'）。

## 5. 统计口径（判断力评分）

| 组 | 口径 | 含义 |
|---|---|---|
| follow 组 | positions 全量：笔数/胜率/平均 ret/累计 pnl | 实际跟单的结果 |
| skip 组 | 每条 skip 决策的虚拟跟入收益（决策点→首个退出信号或 timeout） | 正=错过盈利（判断失误）/负=躲过亏损（判断正确） |
| counter 组 | −虚拟跟入收益 | 看反的对错率 |
| **机械基线** | 同决策点集全 follow（同进出规则、同名义） | 严格跟卡会怎样 |
| **判断力差值** | 主观组合累计 pnl − 机械基线累计 pnl | 核心指标 |

分层：按 signal_source / symbol / decision 维度切片；late 双口径。

**样本量纪律**：笔数 < 30 时只输出描述统计并明示"样本不足不下结论"（§2.5 同款纪律，
防止三五笔连胜就自认有超额判断力）。

## 6. CLI 与报告接入

```
uv run python -m scripts.paper.decide --pending                 # 列全部待决策点（含编号）
uv run python -m scripts.paper.decide --pick <N> --decision follow|skip|counter [--note]
uv run python -m scripts.paper.settle [--as-of D]               # timeout 强制结算扫描
uv run python -m scripts.paper.reversal <decision_id> --reason "... "
uv run python -m scripts.paper.stats [--symbol S] [--exclude-late]
```

- 每日全池报告末尾新增**"模拟盘"独立段**（不动五级排序）：
  ① 今日触发未录决策点清单（含编号，直接照抄到 decide --pick）；
  ② 待平仓决策点（exit 信号已触发未决）；
  ③ open 仓位浮盈快照；④ 累计统计摘要（follow 胜率/累计 pnl/判断力差值）。
- 单股报告 §3 决策点旁加一行"模拟盘：已决 follow（+xxx）/待决"提示。
- **每日管线接入点**：settle 与待决清单由 report 生成时顺带计算（纯读），不进
  daily 信号链；decision 录入独立手触发。

## 7. 明确不做（边界）

1. 不做盘中/实时：全部盘后决策、收盘价成交；
2. 不做自动跟单：决策必须人工录入（自动化=没有判断可测）；
3. counter 不开空仓（A 股约束），仅标记与反向虚拟评价；
4. 不与真实资金混算：模拟盘统计永不并入 executions/收益报告；
5. LLM 不参与：决策是人录的，统计是 Python 算的（LLM 边界）；
6. 不做多空双向/杠杆/分批加减仓（v1 单向多头、一笔一仓）；分批留 v2。

## 8. 给评审 Agent 的重点问题（按风险排序）

1. **并发名义无上限**：同日多信号触发可开无限笔（每笔独立名义，本金仅展示基准）。
   是否需要设并发仓位上限（如 10 笔）？设了的话"满仓时新信号只能 skip"会污染判断
   统计（资金约束混入）——v1 倾向不设，是否接受？
2. **决策窗口 T+1 收盘**：反作弊与实操便利的平衡点是否合适？（更严：T 日 24:00；
   更松：T+2）
3. **box_entry 去抖**：去抖已入 v1（5 日，§2.1）；参数是否合适？（横盘期 5 日去抖
   后仍可能每月 2–3 个决策点/股，是否可接受？）
4. **exit 信号集是否充分**：证伪线 + 右侧 stopped_out 之外，是否要加"档位深度脱离"
   （如收盘跌破 entry 档下沿 ×(1−x%)）作为盈亏平衡退出？不加则 timeout 60 日会承接
   大量无退出信号的仓位。
5. **skip 虚拟收益的退出语义**：skip 的评价窗口用"该信号若跟入后的首个退出信号日"，
   但 skip 后的退出信号可能同样被 skip——机械基线的退出是否也应全跟？（v1：是，
   基线完全机械化，与主观行为无关。）
6. **late 双口径的默认展示**：报告默认含 late 还是剔除？（v1 倾向默认含、标注数。）
7. **表名/模块名**：`paper_decisions`/`paper_positions` + `scripts/paper/` 是否与
   现有命名冲突或歧义（paper vs 模拟 vs 回测 backtest 的边界）？

## 9. 实施切分（评审通过后）

| # | 任务 | 交付 |
|---|---|---|
| 1 | migration 0011（两表）+ db checklist | 表 |
| 2 | config/paper.yaml + 加载 | 参数 |
| 3 | scripts/paper/decide.py（pending 扫描 + 录入 + 反作弊校验） | 录入 |
| 4 | scripts/paper/settle.py（exit 信号/timeout/manual/reversal） | 结算 |
| 5 | scripts/paper/stats.py（三组 + 机械基线 + 差值） | 统计 |
| 6 | report.py 接入模拟盘段（全池日报 + 单股提示行） | 报告 |
| 7 | tests（决策点枚举/窗口校验/结算状态机/基线对照/冲正/报告段） | 测试 |
| 8 | 文档：database_schema/system_design/execution_log/handoff | 留痕 |

预估规模：~700 行实现 + ~400 行测试，1–2 次会话。
