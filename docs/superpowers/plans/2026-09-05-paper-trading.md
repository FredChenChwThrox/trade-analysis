# 模拟盘（信号决策力实验）实施计划

> **For agentic workers:** 本计划由本会话内联执行（executing-plans 模式），步骤用 checkbox 跟踪。
> 设计依据：`docs/superpowers/specs/2026-09-05-paper-trading-design.md`（v2，评审修订版）——
> 实施遇歧义以设计文档为准。

**Goal:** 信号触发点人工录 follow/skip/counter 决策，收盘价成交、对称退出+timeout 兜底，统计三组结果并对照"全跟"机械基线，输出判断力差值。

**Architecture:** migration 0011 两表（paper_decisions append-only 决策流水 / paper_positions 仓位状态机）+ `scripts/paper/` 四模块（common 决策点枚举与公共 / decide 录入 / settle 结算 / stats 统计）+ report.py 接入全池日报"模拟盘"段。与 executions、signal 链、backtest 包完全隔离。

**Tech Stack:** Python 3.12 / sqlite3 / numpy-free（纯 stdlib + yaml）；测试 pytest。

## Global Constraints

- 价格列 TEXT 定点（同 executions）；ret/pnl REAL（展示级）
- entry_adj/exit_adj 不落库；结算时按结算时点 daily_bars 因子两日现取（§3.3 偏差声明）
- 事件化规则：仅状态转变日产生决策点；falsification 用确认日 triggered=1（breached_today 非事件字段）；tier 用"当日 triggered 且前一条存在行 state≠triggered"；box 仅 buy_zone 转变 + 5 交易日冷却
- 一股一仓；open 期间同股 entry 点仅允许 skip 且 constraint_tag='single_position'
- 停牌：信号日必有 bar；结算/timeout 顺延至下一有 bar 日；hold_days 按交易日历含停牌
- 反作弊：价格系统取不可自填；窗口 T 盘后→T+1 收盘（超窗 late）；append-only 冲正；快照冻结
- LLM 不参与；不写 executions；不进 daily 信号链；报告段纯读
- 全部命令 `uv run`；测试 `uv run pytest -q` 全绿；每任务一提交

---

### Task 1: migration 0011 + config

**Files:**
- Create: `scripts/pipeline/migrations/0011_paper_trading.sql`
- Create: `config/paper.yaml`
- Modify: `tests/test_db.py`（migration 清单 +0011）
- Test: `tests/test_db.py::test_migrate_idempotent`

**Interfaces:** 表结构见设计 §3.1/§3.2（v2：TEXT 价格、constraint_tag、deep_exit_line、无 adj 列）。config 键：notional_per_trade=100000 / max_hold_days=60 / decision_window_days=1 / box_entry_debounce_days=5 / deep_exit_pct=0.05。

**Steps:**
- [ ] 写 0011 SQL（两表，字段按设计 §3.1/§3.2 verbatim）
- [ ] config/paper.yaml（§3.4 六键）
- [ ] test_db 清单加 `0011_paper_trading.sql`，版本断言至 11
- [ ] `uv run pytest -q tests/test_db.py` → PASS；`uv run python -c "from scripts.pipeline.db import connect,migrate; c=connect('data/market.db'); print(migrate(c))"` → `['0011_paper_trading.sql']`
- [ ] Commit: `feat(paper): migration 0011 两表 + paper.yaml 配置`

### Task 2: scripts/paper/common.py — 参数、价格、决策点枚举

**Files:**
- Create: `scripts/paper/__init__.py`（含模块 docstring：与 backtest 边界声明）
- Create: `scripts/paper/common.py`
- Test: `tests/test_paper.py::TestEnumerate*`

**Interfaces:**
```python
def load_config(db_path=None) -> dict            # config/paper.yaml，缺文件用默认
def get_bar(conn, symbol, date) -> sqlite3.Row | None        # close_raw/factor/trading_status
def next_bar_date(conn, symbol, after_date) -> str | None    # 顺延用
def trading_days_between(conn, d1, d2) -> int    # 交易日历日数（含端点差，含停牌）
def is_late(conn, decision_date, now_date) -> bool  # now 晚于 T 的下一交易日 → late
def enumerate_decision_points(conn, as_of) -> list[dict]
# 返回：[{symbol, decision_date, decision_type: entry|exit, signal_source,
#         signal_snapshot_json, close_used: str|None,
#         constraint_tag: 'single_position'|None, pick_id: int}]
# pick_id = 展示序号（1 起，按 date+symbol 排序）
def already_decided(conn, symbol, date, dtype, source) -> bool
```

枚举规则（关键算法）：
- tier_triggered：该股该信号按日序扫，`state='triggered'` 且**上一条存在行** `state != 'triggered'`（缺行=延续前值）
- right_side：`state='confirmed'`（转移行天然唯一）
- falsification_breach：`triggered=1`（确认日，`state='active'`）
- box_entry：`state='buy_zone'` 且上一条存在行 state≠'buy_zone'，且距上一条 `signal_source='box_entry'` 的 paper_decisions（该股）> debounce 交易日
- exit 事件（逐 open position）：falsification triggered=1 日 / right_side state='stopped_out' 日 / 首个 close_raw < deep_exit_line 的 bar 日（各日期需 ≥ entry_date）
- 已 decided（UNIQUE 四元组命中 paper_decisions）→ 不列
- open position 存在的股的 entry 点 → constraint_tag='single_position'，仅可 skip
- timeout 不列（settle 自动，非决策）

**Steps:**
- [ ] 写测试（tmp DB + 手插 daily_bars/signal_facts/watchlist）：tier 前日扫描（连续 triggered 只首日一点）、falsification watch 态无点确认日有点、box 去抖、deep_exit 首破日、已决不重列、single_position 打标
- [ ] `uv run pytest -q tests/test_paper.py -k Enumerate` → FAIL
- [ ] 实现 common.py
- [ ] PASS；Commit: `feat(paper): 决策点枚举与公共层（事件化/去抖/结构性skip）`

### Task 3: decide.py — 录入

**Files:**
- Create: `scripts/paper/decide.py`
- Test: `tests/test_paper.py::TestDecide*`

**Interfaces:**
```python
def record_decision(conn, pt: dict, decision: str, note: str, now_dt: str,
                    cfg: dict) -> int   # 返回 decision id；违反规则抛 ValueError
# 校验：exit 点 decision 仅 follow|skip（counter 非法）；单仓约束 follow/counter → ValueError
# late 计算（is_late）；follow-entry → 建 paper_positions 行（deep_exit_line 冻结，
# quantity = notional//close//100*100，至少 100 股不足则报错提示加大 notional）
```
CLI：`decide --pending` / `decide --pick N --decision D [--note]`（pick 序号来自 enumerate 排序）。

**Steps:**
- [ ] 测试：正常 follow 入仓（股数取整百）、counter 非法 on exit、单仓 follow 拒绝、skip 自动打标、T+2 录入 late=true、重复录入冲突
- [ ] FAIL → 实现 → PASS → Commit: `feat(paper): 决策录入（窗口/单仓/结构性skip）`

### Task 4: settle.py — 结算

**Files:**
- Create: `scripts/paper/settle.py`
- Test: `tests/test_paper.py::TestSettle*`

**Interfaces:**
```python
def run_settle(conn, cfg, now_date) -> list[dict]   # timeout 扫描 + 已录 exit-follow 落账
def manual_close(conn, symbol, reason, now_date, cfg) -> int
def reversal(conn, decision_id, reason, now_date, cfg) -> int
def _settle_position(conn, pos, exit_date, exit_source, cfg) -> None
# ret = (exit_close*f_exit)/(entry_close*f_entry) - 1（两因子取结算时点库内值）
# timeout：trading_days_between(entry, today) >= max_hold_days → exit_date=next_bar_date
# 停牌顺延：exit_date 无 bar → next_bar_date
```
CLI：`settle [--as-of D]` / `manual-close --symbol S --reason R` / `reversal --id N --reason R`。

**Steps:**
- [ ] 测试：exit-follow 落账（ret/pnl/hold_days）、跨除权 ret 用因子正确、timeout 触发与停牌顺延、manual 单列 source、reversal 强制平仓、已 close 不重复结算
- [ ] FAIL → 实现 → PASS → Commit: `feat(paper): 结算（退出信号/timeout顺延/manual/reversal）`

### Task 5: stats.py — 统计

**Files:**
- Create: `scripts/paper/stats.py`
- Test: `tests/test_paper.py::TestStats*`

**Interfaces:**
```python
def compute_stats(conn, cfg, *, exclude_late=False, symbol=None) -> dict
# {follow: {n, wins, winrate, avg_ret, cum_pnl, open_n},
#  skip: {n, autonomous_n, structural_n, missed_ret, avoided_ret},
#  counter: {n, correct_n, ...},
#  baseline: {n, cum_pnl, winrate},
#  judgement_diff: cum_pnl_follow_open_mtm + closed − baseline_cum_pnl,
#  late_count, sample_warning: bool(n<30)}
# 虚拟收益（skip/counter/baseline 共用）：entry@决策点收盘 → 基线退出日收盘
#   （该决策点后首个 falsification triggered=1 / stopped_out / deep_exit 破线日；
#    无 → 最新 bar 日 MTM）；未满仓约束不影响基线（全跟）
def format_stats(s: dict) -> str   # CLI 文本
```
CLI：`stats [--symbol S] [--exclude-late]`。

**Steps:**
- [ ] 测试：基线=全点虚拟收益合计、判断力差值=follow MTM 组−基线、结构性 skip 不进自主 skip 分母、<30 笔 sample_warning、late 剔除口径
- [ ] FAIL → 实现 → PASS → Commit: `feat(paper): 统计（三组+机械基线+判断力差值）`

### Task 6: report.py 接入模拟盘段

**Files:**
- Modify: `scripts/pipeline/report.py`（render_daily_report 末尾追加段；build_symbol_report §3 后加一行提示）
- Test: `tests/test_report.py::test_daily_report_paper_section`（新增）

**Interfaces:**
```python
def _paper_section(conn, trade_date, cfg) -> list[str]   # 纯读，行列表
# ①今日未决 entry/exit 点 ②open 仓位浮盈 ③累计摘要（含 late N）
```
单股：§3 决策点列表后追加 `- 模拟盘: 已决 follow / 待决 / 无记录` 一行（有记录才显示）。

**Steps:**
- [ ] 测试：有未决点时报表含"模拟盘"段与编号行；无任何记录时报表含"（模拟盘无记录）"或整段省略——取后者（省略）
- [ ] 实现 → `uv run pytest -q tests/test_report.py` PASS → 全量 pytest → Commit: `feat(paper): 全池日报模拟盘段 + 单股提示行`

### Task 7: 文档 + 提交

**Files:** `docs/database_schema.md`（两表节+速查）、`docs/system_design.md`（§7 表清单）、`docs/handoff.md`（补记）、`docs/execution_log.md`（实施记录）

**Steps:**
- [ ] 全量 `uv run pytest -q` 全绿
- [ ] 真实库 migrate 0011；`decide --pending` 冒烟（当前无 09-05 触发点则输出空清单）
- [ ] 四文档更新 + Commit: `feat(paper): 模拟盘落地——决策/结算/统计/报告段（0011）`

## Self-Review

- Spec 覆盖：§2.1 枚举（T2）、§2.2 退出（T4）、§2.3 反作弊（T3 校验+T4 冲正）、§2.4 停牌（T2 next_bar/T4 顺延）、§3 表（T1）、§5 统计（T5）、§6 报告（T6）——全覆盖，无缺口
- 占位符：无 TBD；测试步骤含具体断言目标
- 类型一致：enumerate 返回字段与 decide 消费一致（pt dict 键名）；settle/stats 均从 common 取 cfg
