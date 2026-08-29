"""报告生成器（D2.6，设计 §6.1-6.4、§9.5、§8.1 步骤 7）。

对 watchlist 每只股票生成单股报告 `reports/{symbol}/{trade_date}.md`
（§6.2 八段结构，r2 Phase 1 增"5. 日历与消息面"），再汇总全池日报 `reports/daily/{trade_date}.md`
（§6.3 五级确定性排序，不由 LLM 排序）。

契约：
- **模板化生成，无 LLM 摘要**：所有数字来自结构化输入（signal_facts
  details_json / indicators_* / daily_bars / strategy_card_versions，§6.4）。
- **语言纪律**：只用"触发、证伪、释放仓位、冻结、待确认"等规则语言；
  无触发明写"今日无决策点"；关键数字标注来源与截止日期。
- **衰竭信号段必须带锚点明细**（§5.2 ⚠️：锚点日期、起点日期、计算值，
  供人工核对数周后再考虑调参）。
- **report_runs**：每份报告一行（run_id/as_of/card_version_id/rule_version/
  config_hash/input_snapshot_json/status/revision/file_path）；同日重跑不删旧行，
  生成 revision 新行并覆盖同名文件（§9.5 降级：revision 序号记入报告头与日志）。
- 非交易日门禁的股票跳过单股报告，在全池日报"非交易日"段列出。

CLI：
    uv run python -m scripts.pipeline.report --date 2026-08-07 [--symbol S] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal
from pathlib import Path

from scripts.pipeline.calendar_check import (
    STATUS_INCOMPLETE,
    STATUS_NON_TRADING,
    STATUS_OK,
    STATUS_SOURCE_MISSING,
    STATUS_SUSPENDED,
    check_symbol_day,
)
from scripts.pipeline.db import DEFAULT_DB_PATH, ROOT, connect, utc_now
from scripts.signals import cards as card_mod
from scripts.signals import corporate_action as ca_mod
from scripts.signals import calendar_due  # r2 Phase 1：日历到期提醒（单股过滤见 relevant_to_symbol）
from scripts.signals import event_link  # r2 Phase 3：消息面 effective 解析
from scripts.llm import labels  # r2 Phase 3：标签中文呈现（报告/人审页统一映射）
from scripts.signals.common import RULE_VERSION as SIGNALS_RULE_VERSION
from scripts.signals.common import WEEKLY_SIGNALS, load_params

REPORT_RULE_VERSION = "report_v1"
REPORTS_ROOT = ROOT / "reports"

# 语言纪律扫描词（§6.4；测试也据此扫描报告文本）
BANNED_WORDS = ["看涨", "看跌", "建议买入", "建议卖出", "建议持有", "预测涨跌"]

PROXIMITY_PCT_DEFAULT = 0.03


# ---------------------------------------------------------------- 结果结构

@dataclass
class SymbolReport:
    symbol: str
    trade_date: str
    status: str = "complete"          # complete / degraded / incomplete
    reasons: list[str] = field(default_factory=list)
    card_version_id: str | None = None
    markdown: str = ""
    file_path: str = ""
    revision: int = 1
    priority: int = 5                  # §6.3 五级
    headline: str = ""
    sort_reason: str = ""
    sort_distance: float | None = None  # 同级排序用距边界百分比
    decision_points: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)


@dataclass
class ReportResult:
    trade_date: str
    run_id: str
    symbols: list[SymbolReport] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (symbol, 原因)
    daily_path: str = ""
    daily_revision: int = 1
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [f"report run {self.run_id} (date={self.trade_date})"]
        for s in self.symbols:
            lines.append(f"  {s.symbol}: {s.status} P{s.priority} revision={s.revision} "
                         f"-> {s.file_path}" + (f"（{'；'.join(s.reasons)}）" if s.reasons else ""))
        lines.extend(f"  跳过 {sym}: {why}" for sym, why in self.skipped)
        lines.append(f"  全池日报 revision={self.daily_revision} -> {self.daily_path}")
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------- 数据装配

def _fact(conn: sqlite3.Connection, symbol: str, signal: str,
          as_of: str) -> dict | None:
    """≤ as_of 最新一条某类信号事实（含解析后的 details）。"""
    r = conn.execute(
        """
        SELECT observed_on, state, triggered, active_until, anchor_id, details_json
        FROM signal_facts
        WHERE symbol = ? AND signal = ? AND observed_on <= ?
        ORDER BY observed_on DESC LIMIT 1
        """,
        (symbol, signal, as_of),
    ).fetchone()
    if r is None:
        return None
    return {
        "observed_on": r["observed_on"], "state": r["state"],
        "triggered": r["triggered"], "active_until": r["active_until"],
        "anchor_id": r["anchor_id"],
        "details": json.loads(r["details_json"]) if r["details_json"] else {},
    }


def _latest_bar(conn: sqlite3.Connection, symbol: str, as_of: str):
    return conn.execute(
        "SELECT * FROM daily_bars WHERE symbol = ? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 1",
        (symbol, as_of),
    ).fetchone()


def _latest_completed_week(conn: sqlite3.Connection, symbol: str,
                           as_of: str) -> str | None:
    r = conn.execute(
        "SELECT MAX(week_end_date) AS d FROM weekly_bars "
        "WHERE symbol = ? AND week_end_date <= ?",
        (symbol, as_of),
    ).fetchone()
    return r["d"] if r else None


def _latest_anchors(conn: sqlite3.Connection, symbol: str,
                    as_of: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT * FROM weekly_anchors
        WHERE symbol = ? AND as_of = (
            SELECT MAX(as_of) FROM weekly_anchors WHERE symbol = ? AND as_of <= ?)
        """,
        (symbol, symbol, as_of),
    ).fetchall()
    return {r["anchor_type"]: r for r in rows}


def _pending_conversion_drafts(conn: sqlite3.Connection,
                               symbol: str) -> list[dict]:
    """公司行为换算 draft 待确认（§5.4b 第三步，日报最高优先级决策点）。"""
    out = []
    for r in conn.execute(
            "SELECT card_version_id, input_snapshot_json FROM strategy_card_versions "
            "WHERE symbol = ? AND status = 'draft'", (symbol,)).fetchall():
        snap = json.loads(r["input_snapshot_json"] or "{}")
        conv = snap.get("conversion")
        if conv:
            out.append({"card_version_id": r["card_version_id"], "conversion": conv})
    return out


def _pct(frac: float | None, digits: int = 1) -> str:
    return "—" if frac is None else f"{frac * 100:.{digits}f}%"


def _wan(v: float | None) -> str:
    return "—" if v is None else f"{v / 10000:,.1f} 万"


def _f(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


# ---------------------------------------------------------------- 单股报告（§6.2 八段）

def build_symbol_report(conn: sqlite3.Connection, symbol: str, trade_date: str,
                        *, params: dict, config_hash: str,
                        indicators_config_hash: str | None = None,
                        run_id: str, revision: int) -> SymbolReport:
    rep = SymbolReport(symbol=symbol, trade_date=trade_date)
    gate = check_symbol_day(conn, symbol, trade_date)
    bar_today = conn.execute(
        "SELECT * FROM daily_bars WHERE symbol = ? AND trade_date = ?",
        (symbol, trade_date)).fetchone()
    bar = bar_today or _latest_bar(conn, symbol, trade_date)
    cutoff = bar["trade_date"] if bar else None
    ind = conn.execute(
        "SELECT * FROM indicators_daily WHERE symbol = ? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 1",
        (symbol, trade_date)).fetchone()
    card = card_mod.load_active_card(conn, symbol, trade_date)
    rep.card_version_id = card.card_version_id if card else None
    next_review_at = None
    if card:
        r = conn.execute(
            "SELECT next_review_at FROM strategy_card_versions "
            "WHERE card_version_id = ?", (card.card_version_id,)).fetchone()
        next_review_at = r["next_review_at"] if r else None
    week_end = _latest_completed_week(conn, symbol, trade_date)
    anchors = _latest_anchors(conn, symbol, week_end or trade_date)
    suspensions = ca_mod.unresolved_suspensions(conn, symbol)
    conv_drafts = _pending_conversion_drafts(conn, symbol)

    facts = {sig: _fact(conn, symbol, sig, trade_date) for sig in (
        ["tier_proximity", "tier_triggered", "falsification_breach",
         "box_position", "ma_comparison", "right_side", "daily_watch",
         "accumulation"])}
    weekly_facts = {sig: _fact(conn, symbol, sig, trade_date)
                    for sig in WEEKLY_SIGNALS}

    # ---- 状态判定（§2.5：关键数据缺失输出 incomplete/degraded，不伪装）
    if gate.status == STATUS_INCOMPLETE:
        rep.status, rep.priority = "incomplete", 1
        rep.reasons.append(gate.reason or "incomplete")
    elif gate.status in (STATUS_SOURCE_MISSING, STATUS_SUSPENDED):
        rep.status, rep.priority = "degraded", 1
        rep.reasons.append(gate.reason or gate.status)
    if bar is None:
        rep.status, rep.priority = "incomplete", 1
        rep.reasons.append("daily_bars 无数据")
    if bar is not None and ind is None:
        rep.status = "degraded" if rep.status == "complete" else rep.status
        rep.priority = min(rep.priority, 1)
        rep.reasons.append("indicators_daily 缺失（指标未重算）")
    if gate.status == STATUS_OK and card is None:
        rep.status = "degraded" if rep.status == "complete" else rep.status
        rep.reasons.append("no_active_card（卡片相关监测未运行，§2.5）")
    if cutoff and cutoff < trade_date and gate.status == STATUS_OK:
        rep.status = "degraded" if rep.status == "complete" else rep.status
        rep.reasons.append(f"行情截止 {cutoff} 早于报告日 {trade_date}")

    close = Decimal(str(bar["close_raw"])) if bar else None

    # ================= 决策点（§6.2 第 3 段）=================
    dp: list[str] = []
    for s in suspensions:
        dp.append(f"[公司行为冻结] {s['action_type']} ex_date={s['ex_date']} 起，"
                  f"卡片触发挂起（signal_facts suspended_corporate_action，§5.4b；"
                  f"来源 signal_facts，截止 {trade_date}）")
    for d in conv_drafts:
        c = d["conversion"]
        dp.append(f"[换算 draft 待确认] {d['card_version_id']}（{c.get('action_type')} "
                  f"ex_date={c.get('ex_date')}，来源版本 {c.get('source_card_version_id')}；"
                  f"确认激活后监测恢复，来源 strategy_card_versions，截止 {trade_date}）")
    if next_review_at and next_review_at < trade_date:
        dp.append(f"[卡片复核逾期] next_review_at={next_review_at} 已过，"
                  f"生成复核提醒（不自动延后，来源 strategy_card_versions，截止 {trade_date}）")
    fb = facts["falsification_breach"]
    if fb and fb["state"] == "active":
        det = fb["details"]
        dp.append(f"[证伪线有效跌破] 收盘 {det.get('close_raw')} ≤ 跌破阈值 "
                  f"{det.get('breach_threshold')}（证伪线 {det.get('invalidation_line')} "
                  f"×(1−{det.get('breach_pct')})，连续 {det.get('consecutive_breach_days')}/"
                  f"{det.get('confirm_days')} 日，{'确认日' if fb['triggered'] else '持续有效'}；"
                  f"来源 signal_facts falsification_breach @ {fb['observed_on']}）")
    tt = facts["tier_triggered"]
    if tt and tt["observed_on"] == trade_date and tt["triggered"] == 1:
        in_zone = [t for t in tt["details"].get("tiers", []) if t.get("in_zone")]
        for t in in_zone:
            if (t.get("tier") or 1) >= 2 and not t.get("signals_met"):
                continue
            dp.append(f"[档位触发 T{t['tier']}] 收盘 {tt['details'].get('close_raw')} "
                      f"进入价区 [{t['zone_low']}, {t['zone_high']}]"
                      + ("（第一档，无附加信号要求）" if (t.get("tier") or 1) == 1 else
                         f"（同锚点活跃衰竭信号 {tt['details'].get('active_count')} 项 "
                         f"≥ {tt['details'].get('min_active_signals')}）")
                      + f"；来源 signal_facts tier_triggered @ {trade_date}，卡 {rep.card_version_id}）")
    if tt and tt["observed_on"] == trade_date and tt["state"] == "pending_signals":
        det = tt["details"]
        dp.append(f"[档位触发待确认] 收盘已进入价区但同锚点活跃衰竭信号 "
                  f"{det.get('active_count')} 项 < {det.get('min_active_signals')}，"
                  f"不触发（来源 signal_facts tier_triggered @ {trade_date}）")
    rs = facts["right_side"]
    if rs and rs["observed_on"] == trade_date and rs["state"] in (
            "confirmed", "invalidated", "expired"):
        det = rs["details"]
        label = {"confirmed": "右侧确认成立", "invalidated": "右侧确认失效",
                 "expired": "右侧确认窗口过期"}[rs["state"]]
        dp.append(f"[{label}] 关键位 {det.get('trigger_level')}，{det.get('reason')}；"
                  f"来源 signal_facts right_side @ {trade_date}）")
    bp = facts["box_position"]
    if bp and bp["observed_on"] == trade_date and bp["triggered"] == 1:
        dp.append(f"[波段箱体 {bp['state']}] 收盘 {bp['details'].get('close_raw')}，"
                  f"存档边界 {bp['details'].get('boundaries')}；"
                  f"来源 signal_facts box_position @ {trade_date}）")
    rep.decision_points = dp

    # ================= 优先级（§6.3）=================
    prox = facts["tier_proximity"]
    min_dist: float | None = None
    if prox:
        dists = [t.get("distance_to_nearest_boundary_pct")
                 for t in prox["details"].get("tiers", [])
                 if t.get("distance_to_nearest_boundary_pct") is not None]
        min_dist = min(dists) if dists else None
    rep.sort_distance = min_dist
    if rep.priority == 1:
        rep.headline = f"{gate.status}（{gate.reason}）" if gate.status != STATUS_OK \
            else "；".join(rep.reasons)
        rep.sort_reason = "数据异常优先；同级按 symbol"
    elif suspensions or conv_drafts or (fb and fb["state"] == "active") or (
            next_review_at and next_review_at < trade_date):
        rep.priority = 2
        rep.headline = "；".join(d.split("]")[0].lstrip("[") for d in dp)
        rep.sort_reason = "证伪/复核逾期/换算待确认；同级按 symbol"
    elif (tt and tt["observed_on"] == trade_date and tt["triggered"] == 1) or (
            rs and rs["observed_on"] == trade_date
            and rs["state"] in ("confirmed", "invalidated")) or (
            bp and bp["observed_on"] == trade_date and bp["triggered"] == 1):
        rep.priority = 3
        rep.headline = "；".join(d.split("]")[0].lstrip("[") for d in dp if d.startswith(
            ("[档位触发", "[右侧", "[波段箱体"))) or "已确认决策点"
        rep.sort_reason = (f"同级按距边界百分比（{_pct(min_dist)}）、symbol"
                           if min_dist is not None else "同级按 symbol")
    elif prox and prox["observed_on"] == trade_date and prox["state"] == "active":
        rep.priority = 4
        near = [t for t in prox["details"].get("tiers", []) if t.get("within_proximity")]
        t0 = near[0] if near else {}
        bound = "上沿" if t0.get("nearest_boundary") == t0.get("zone_high") else "下沿"
        rep.headline = (f"距 T{t0.get('tier')} {bound} {t0.get('nearest_boundary')} "
                        f"还差 {_pct(t0.get('distance_to_nearest_boundary_pct'))}")
        rep.sort_reason = f"同级按距边界百分比（{_pct(min_dist)}）、symbol"
    else:
        rep.priority = 5
        rep.headline = "普通状态更新"
        rep.sort_reason = "同级按 symbol"

    # ================= 渲染八段 =================
    L: list[str] = []
    a = L.append
    wl = conn.execute("SELECT name FROM watchlist WHERE symbol = ?",
                      (symbol,)).fetchone()
    name = wl["name"] if wl else ""
    a(f"# {symbol} {name} 单股报告 — {trade_date}")
    a("")
    a("> 模板化生成（无 LLM 摘要，§6.4）；所有数字来自结构化输入。"
      + (f"revision {revision}（同日重跑生成新 revision 行并覆盖同名文件，§9.5）。"
         if revision > 1 else ""))
    a("")

    # ---- 1. 运行状态
    a("## 1. 运行状态")
    a("")
    a(f"- 数据截止: {cutoff or '无'}（来源 daily_bars；报告日 {trade_date}）")
    a(f"- 当日门禁: {gate.status}" + (f"（{gate.reason}）" if gate.reason else "")
      + "（来源 trading_calendar + daily_bars + index_bars 交叉校验）")
    a(f"- 报告状态: {rep.status}" + (f"（{'；'.join(rep.reasons)}）" if rep.reasons else ""))
    if card:
        a(f"- 当前卡片: `{card.card_version_id}`（生效 [{card.effective_from}, "
          f"{card.effective_to or '开口'})，来源 strategy_card_versions）")
    else:
        a("- 当前卡片: 无 active 版本（§2.5：卡片相关信号不输出，不猜）")
    a(f"- rule_version: {SIGNALS_RULE_VERSION}/{REPORT_RULE_VERSION}；"
      f"config_hash: {config_hash[:12]}…（config/signals.yaml 内容哈希）")
    a(f"- run_id: {run_id}；revision: {revision}")
    a("")

    # ---- 2. 当前定位
    a("## 2. 当前定位（不复权口径）")
    a("")
    if close is not None:
        a(f"- 现价: {close}（来源 daily_bars 收盘，截止 {cutoff}）")
        if card and card.tiers:
            in_zone = [t for t in card.tiers
                       if t["zone_low"] <= close <= t["zone_high"]]
            if in_zone:
                t = in_zone[0]
                a(f"- 所处档位: T{t['tier']} 价区 [{t['zone_low']}, {t['zone_high']}] 内"
                  f"（来源 strategy_card_versions + daily_bars，截止 {cutoff}）")
            elif prox and prox["details"].get("tiers"):
                dists = sorted(prox["details"]["tiers"],
                               key=lambda x: x.get("distance_to_nearest_boundary_pct") or 9)
                t = dists[0]
                bound = "上沿" if t.get("nearest_boundary") == t.get("zone_high") else "下沿"
                a(f"- 所处档位: 价区外；距最近边界 T{t.get('tier')} {bound} "
                  f"{t.get('nearest_boundary')} 为 "
                  f"{_pct(t.get('distance_to_nearest_boundary_pct'))}"
                  f"（来源 signal_facts tier_proximity @ {prox['observed_on']}）")
        elif not card:
            a("- 所处档位: 无 active 卡片，不计算（§2.5）")
        if bp:
            a(f"- 箱体位置: {bp['state']}（存档边界 "
              f"{bp['details'].get('boundaries')}；来源 signal_facts box_position "
              f"@ {bp['observed_on']}）")
    else:
        a("- 现价: 无数据（daily_bars 空，§2.5）")
    a("")

    # ---- 3. 决策点
    a("## 3. 决策点")
    a("")
    if dp:
        for d in dp:
            a(f"- {d}")
    else:
        a("- 今日无决策点。")
    a("")

    # ---- 4. 观察点
    a("## 4. 观察点（距扳机临近度）")
    a("")
    prox_pct = float(params["daily_watch"]["tier_proximity_pct"])
    if prox and prox["details"].get("tiers"):
        for t in sorted(prox["details"]["tiers"],
                        key=lambda x: x.get("tier") or 0):
            d = t.get("distance_to_nearest_boundary_pct")
            bound = "上沿" if t.get("nearest_boundary") == t.get("zone_high") else "下沿"
            tag = "≤3% 临近阈值内" if t.get("within_proximity") else "超出 3% 临近阈值"
            a(f"- 档位 T{t.get('tier')}: 现价距{bound} {t.get('nearest_boundary')} "
              f"还差 {_pct(d)}（{tag}；来源 signal_facts tier_proximity "
              f"@ {prox['observed_on']}）")
    elif card is None:
        a("- 档位: 无 active 卡片，不计算（§2.5）")
    if fb and fb["details"].get("invalidation_line"):
        det = fb["details"]
        line = Decimal(str(det["invalidation_line"]))
        if close is not None:
            dist = (close - line) / line
            a(f"- 证伪线: 现价{'高于' if dist >= 0 else '低于'}证伪线 {line} "
              f"{_pct(abs(dist))}；连续跌破 {det.get('consecutive_breach_days')}/"
              f"{det.get('confirm_days')} 日（来源 signal_facts falsification_breach "
              f"@ {fb['observed_on']}）")
    du = weekly_facts["dry_up"]
    if du and du["details"].get("threshold_volume") is not None:
        det = du["details"]
        cur_v, thr = det.get("current_volume"), det["threshold_volume"]
        if cur_v is not None:
            gap = cur_v - thr
            a(f"- 干涸阈值: 当前完成周调整量 {_wan(cur_v)}，干涸阈值 "
              f"{_wan(thr)}（基数 {_wan(det.get('base_mean'))} × "
              f"{det.get('vol_ratio_threshold')}）；"
              + (f"距阈值还差 {_wan(gap)}（未满足）" if gap > 0 else
                 f"已低于阈值 {_wan(-gap)}（满足）")
              + f"；来源 signal_facts dry_up @ {du['observed_on']}）")
    if card and card.trigger_level is not None and close is not None:
        p = params["right_side"]
        line = card.trigger_level * (Decimal("1") + Decimal(str(p["breakout_pct"])))
        dist = (line - close) / line
        a(f"- 右侧: 突破线 {line:.2f}（触发位 {card.trigger_level} ×(1+"
          f"{p['breakout_pct']:.0%})）；现价距突破线还差 {_pct(dist)}；"
          f"状态机最近转换 {rs['state'] if rs else 'idle'}（terminal 后回 idle）"
          f"（来源 strategy_card_versions + signal_facts right_side，截止 {trade_date}）")
    acc = facts["accumulation"]
    if acc:
        det = acc["details"]
        acc_cn = {"idle": "无活跃形态", "watching": "放量破位已见，等待缩量横盘",
                  "consolidating": "缩量横盘中", "confirmed": "放量突破确认",
                  "failed": "形态失效"}.get(acc["state"], acc["state"])
        parts = [f"状态 {acc['state']}（{acc_cn}，reason={det.get('reason')}）"]
        if det.get("breakdown_date"):
            parts.append(f"破位日 {det['breakdown_date']}")
        if det.get("box_low") is not None:
            parts.append(f"箱体(复权收盘) [{_f(det.get('box_low'), 4)}, "
                         f"{_f(det.get('box_high'), 4)}]，试盘 "
                         f"{det.get('probe_count', 0)} 次")
        a(f"- 吸筹形态（⚠️ 参数默认值待人工核对；日 K 代理，无分时/盘口数据；"
          f"仅观察点不进卡片触发，§5.5）: {'；'.join(parts)}"
          f"（来源 signal_facts accumulation @ {acc['observed_on']}）")
    if next_review_at:
        days_left = (date_type.fromisoformat(next_review_at)
                     - date_type.fromisoformat(trade_date)).days
        a(f"- 卡片复核: next_review_at={next_review_at}"
          + (f"（剩余 {days_left} 天）" if days_left >= 0 else
             f"（已逾期 {-days_left} 天，见决策点）")
          + "（来源 strategy_card_versions）")
    ev = conn.execute(
        "SELECT COUNT(*) AS c FROM events e JOIN event_symbols es ON e.event_id = es.event_id "
        "WHERE es.symbol = ?", (symbol,)).fetchone()
    a(f"- 财报/公告窗口与消息: 库内事件 {ev['c']} 条；LLM 消息评价未接入本批，"
      f"消息面按缺失标注（§2.5，截止 {trade_date}）")
    a("")

    # ---- 5. 日历与消息面（r2 Phase 1：日历提醒 + 公司公告；LLM"消息面"子节属 Phase 3）
    a("## 5. 日历与消息面")
    a("")
    cal_all = calendar_due.due_items(conn, trade_date)
    cal_here = calendar_due.relevant_to_symbol(cal_all, symbol)
    a("### 日历提醒（默认 3 日内）")
    a("")
    if cal_here:
        for it in cal_here:
            tail = "——检查头寸是否落在计划档位内"
            if it["kind"] == calendar_due.KIND_CARD_REVIEW:
                a(f"- [{it['date']}] {it['note']}（{it['symbol']}，"
                  f"来源 strategy_card_versions）" + tail)
            else:
                # note 在来源端已自描述（含"财报披露预约/解禁/CPI"等词），不再加 kind 标签
                a(f"- [{it['date']}] {it['note'] or it['kind']}"
                  f"（{it['symbol'] or '宏观'}，来源 event_calendar）" + tail)
    else:
        a("- 默认 3 日内无到期项。")
    a("")
    a("### 公司公告")
    a("")
    # r2 Phase 1：available_at 是 UTC ISO datetime，trade_date 是日期字符串——
    # 必须 substr 日期化比较（直接 available_at <= as_of 按字符串比较恒 False）
    ann = conn.execute(
        """
        SELECT e.title, e.available_at, e.canonical_url
        FROM events e
        JOIN event_symbols es ON e.event_id = es.event_id
        WHERE es.symbol = ? AND e.event_type = 'announcement'
          AND substr(e.available_at, 1, 10) = ?
        ORDER BY e.available_at, e.title
        """, (symbol, trade_date)).fetchall()
    if ann:
        for r in ann:
            a(f"- [{r['available_at'][:10]}] {r['title']} ※ 需读原文"
              f"（成色词以巨潮/交易所 PDF 为准）"
              + (f"（{r['canonical_url']}）" if r["canonical_url"] else ""))
    else:
        a("- 今日无新增公告。")
    a("")
    a("### 消息面（LLM 初判 + 人审后）")
    a("")
    # r2 Phase 3：触发条件 = effective ok（未撤销 dismiss）+ narrative 非空 +
    # available_at ≤ as_of；needs_review 不进段；显示值经人审 amend/upgrade 覆盖。
    msg_events = conn.execute(
        """
        SELECT e.event_id, e.title, e.available_at
        FROM events e
        JOIN event_symbols es ON es.event_id = e.event_id
        JOIN event_assessments a ON a.event_id = e.event_id
             AND a.symbol = '__event__' AND a.assessment_version = 'llm_v1'
        WHERE es.symbol = ? AND substr(e.available_at, 1, 10) <= ?
          AND EXISTS (SELECT 1 FROM event_assessments na
                      WHERE na.event_id = e.event_id AND na.symbol = es.symbol
                        AND na.assessment_version = 'llm_v1' AND na.narrative IS NOT NULL)
        ORDER BY e.available_at DESC LIMIT 20
        """, (symbol, trade_date)).fetchall()
    shown = 0
    for r in msg_events:
        eff = event_link.resolve_effective(conn, r["event_id"])
        view = eff["symbols"].get(symbol, {})
        if view.get("hidden") or view.get("status") != "ok":
            continue
        shown += 1
        a(f"- [{r['available_at'][:10]}] {r['title']}")
        a(f"  标签: {labels.tags_line(view)}（LLM 初判，人工复核后）")
        a(f"  预期差: {view['expectation_gap'] or '待人工补写'}")
        a(f"  叙事: {view['narrative'] or '—'}")
        if view.get("falsification"):
            a(f"  证伪条件: {view['falsification']}")
    if shown == 0:
        a("- 今日无纳入消息面的事件（未评价/未过审/已否决的不冒充展示）")
    # r2 §8.2 价格位置：确定性 join（LLM 不参与）
    prox_tiers = (prox or {}).get("details", {}).get("tiers") or []
    nearest = min((abs(t.get("distance_to_nearest_boundary_pct"))
                   for t in prox_tiers
                   if t.get("distance_to_nearest_boundary_pct") is not None),
                  default=None)
    active_ex = sorted(s for s, f_ in weekly_facts.items()
                       if f_ and f_["observed_on"] == week_end
                       and f_["state"] == "active") if week_end else []
    a(f"- 价格位置: 距最近档边界 "
      f"{_f(nearest) + '%' if nearest is not None else '—（无 active 卡或无临近度数据）'}"
      f"；活跃衰竭信号 {len(active_ex)} 项"
      f"（确定性 join：signal_facts tier_proximity / weekly，LLM 不参与）")
    a("")
    # ---- 6. 衰竭信号（⚠️ §5.2 锚点明细必须带出）
    a("## 6. 衰竭信号")
    a("")
    if week_end and weekly_facts.get("panic"):
        pa, ds = anchors.get("panic_low"), anchors.get("decline_start")
        anchor_id = (weekly_facts["panic"] or {}).get("anchor_id")
        a(f"- 当前完成周: {week_end}；anchor_id={anchor_id}"
          f"（来源 signal_facts/weekly_anchors，⚠️ §5.2 锚点明细供人工核对）")
        if pa:
            a(f"- 恐慌低点锚点: 交易日 {pa['trade_date']}，复权价 {_f(pa['adjusted_price'], 4)} / "
              f"不复权价 {_f(pa['raw_price'], 4)}，fallback={bool(pa['is_fallback'])}"
              f"（weekly_anchors as_of={pa['as_of']}）")
        if ds:
            a(f"- 下跌起点锚点: {ds['trade_date']}，复权收盘 {_f(ds['adjusted_price'], 4)} / "
              f"不复权收盘 {_f(ds['raw_price'], 4)}（weekly_anchors as_of={ds['as_of']}）")
        active = sorted(s for s, f_ in weekly_facts.items()
                        if f_ and f_["observed_on"] == week_end and f_["state"] == "active")
        min_active = params["exhaustion"]["min_active_signals"]
        a(f"- 活跃信号 {len(active)} 项（min_active_signals={min_active}）: {active}")
        a("")
        a("| 信号 | state | triggered | active_until | 原因 |")
        a("|---|---|---|---|---|")
        for sig in WEEKLY_SIGNALS:
            f_ = weekly_facts[sig]
            if f_ and f_["observed_on"] == week_end:
                a(f"| {sig} | {f_['state']} | {f_['triggered']} | "
                  f"{f_['active_until'] or '—'} | {f_['details'].get('reason') or '—'} |")
        a("")
    else:
        a(f"- 无周线信号数据（weekly_bars/signal_facts 缺失，截止 {trade_date}，§2.5）")
        a("")

    # ---- 7. 指标快照
    a("## 7. 指标快照")
    a("")
    if ind is not None:
        a(f"日线（来源 indicators_daily，截止 {ind['trade_date']}，复权口径）：")
        a("")
        fac_row = conn.execute(
            "SELECT price_adj_factor FROM daily_bars WHERE symbol = ? "
            "AND trade_date = ?", (symbol, ind["trade_date"])).fetchone()
        fac = (fac_row["price_adj_factor"] if fac_row else None) or None

        def _fold_line(vals: list) -> str:
            """复权值 ÷ 当日因子折回为不复权口径（§5.4）；因子缺失返回 None。"""
            if not fac or fac <= 0:
                return None
            return " / ".join("—" if v is None else f"{v / fac:.4f}" for v in vals)

        ma_vals = [ind[k] for k in ("ma5", "ma10", "ma20", "ma60", "ma120", "ma250")]
        a(f"- MA5/10/20/60/120/250: {_f(ind['ma5'], 4)} / {_f(ind['ma10'], 4)} / "
          f"{_f(ind['ma20'], 4)} / {_f(ind['ma60'], 4)} / {_f(ind['ma120'], 4)} / "
          f"{_f(ind['ma250'], 4)}")
        ma_fold = _fold_line(ma_vals)
        if ma_fold:
            a(f"- MA 折回（复权 ÷ 当日因子 {_f(fac, 6)} = 不复权口径，可与现价直接比，"
              f"§5.4）: {ma_fold}")
        ma_cmp = facts["ma_comparison"]
        if ma_cmp and ma_cmp["details"].get("mas"):
            mas = ma_cmp["details"]["mas"]
            pos_cn = {"above": "高于", "below": "低于", "at": "持平于"}
            pos = pos_cn.get((mas.get('ma20') or {}).get('position'), "—")
            a(f"- 口径纪律（§5.4）: 复权 MA20 {_f((mas.get('ma20') or {}).get('adjusted'), 4)} "
              f"÷ 当日因子 {_f(ma_cmp['details'].get('price_adj_factor'), 6)} = 折回 "
              f"{_f((mas.get('ma20') or {}).get('raw_equiv'), 4)}，现价{pos}折回值"
              f"（来源 signal_facts ma_comparison @ {ma_cmp['observed_on']}）")
        a(f"- MACD: DIF {_f(ind['dif'], 5)} / DEA {_f(ind['dea'], 5)} / 柱 "
          f"{_f(ind['macd_hist'], 5)}；RSI6/12/24: {_f(ind['rsi6'], 2)} / "
          f"{_f(ind['rsi12'], 2)} / {_f(ind['rsi24'], 2)}")
        a(f"- BOLL: 中 {_f(ind['boll_mid'], 4)} / 上 {_f(ind['boll_upper'], 4)} / "
          f"下 {_f(ind['boll_lower'], 4)} / 带宽 {_f(ind['boll_bandwidth'], 5)}")
        boll_fold = _fold_line([ind["boll_mid"], ind["boll_upper"], ind["boll_lower"]])
        if boll_fold:
            parts = boll_fold.split(" / ")
            a(f"- BOLL 折回（不复权口径，§5.4）: 中 {parts[0]} / 上 {parts[1]} / "
              f"下 {parts[2]}")
        a(f"- 量能: vol_ma5 {_wan(ind['vol_ma5'])} / 20 日均量 {_wan(ind['vol_mean20'])} / "
          f"20 日标准差 {_wan(ind['vol_std20'])}（调整后成交量）")
        a(f"- 基础量: 涨跌幅 {_f(ind['pct_chg'])}% / 振幅 {_f(ind['amplitude'])}%")
        a(f"- PE(TTM): {_f(ind['pe_ttm'], 4)}（pe_status: {ind['pe_status'] or '—'}；"
          f"不复权市值 ÷ TTM 归母净利，来源 indicators_daily）")
    else:
        a("- indicators_daily 无数据（§2.5）")
    if week_end:
        wi = conn.execute(
            "SELECT * FROM indicators_weekly WHERE symbol = ? AND week_end_date = ?",
            (symbol, week_end)).fetchone()
        wb = conn.execute(
            "SELECT close_adj, volume_adj FROM weekly_bars WHERE symbol = ? "
            "AND week_end_date = ?", (symbol, week_end)).fetchone()
        if wi or wb:
            a("")
            a(f"周线（来源 indicators_weekly/weekly_bars，完成周 {week_end}）：")
            a("")
            parts = []
            if wb:
                parts.append(f"复权收盘 {_f(wb['close_adj'], 4)}，调整量 {_wan(wb['volume_adj'])}")
            if wi:
                parts.append(f"RSI12 {_f(wi['rsi12'], 2)}，MACD 柱 {_f(wi['macd_hist'], 5)}")
            a("- " + "；".join(parts))
    a("")

    # ---- 8. 来源与异常
    a("## 8. 来源与异常")
    a("")
    src = (bar["source"] if bar else None) or "—"
    a(f"- 行情: {src}，截止 {cutoff or '无'}；"
      f"{'amount 来源缺失，amt_* 指标为 NULL' if ind and ind['amt_mean20'] is None else '成交额指标可用'}")
    if ind and ind["pe_status"] and "degraded" in (ind["pe_status"] or ""):
        a(f"- PE 降级标注: pe_status={ind['pe_status']}（财报披露日缺失降级，"
          "待披露日来源补齐后消除）")
    es_rows = conn.execute(
        """
        SELECT ea.status AS st, COUNT(*) AS c,
               SUM(CASE WHEN ea.event_study_json LIKE '%"mark": "pending"%'
                        THEN 1 ELSE 0 END) AS p
        FROM event_assessments ea
        JOIN event_symbols es ON ea.event_id = es.event_id
        WHERE es.symbol = ? AND ea.assessment_version = 'event_study_v1'
        GROUP BY ea.status
        """, (symbol,)).fetchall()
    es_total = sum(r["c"] for r in es_rows)
    es_pending = sum(r["p"] or 0 for r in es_rows)
    es_dist = " / ".join(f"{r['st']} {r['c']}" for r in es_rows) or "无"
    a(f"- 消息评价: LLM 消息评价（D3）未接入，消息面评价部分按缺失标注（§2.5）；"
      f"确定性事件研究 event_study_v1 已接入: 库内 {es_total} 条"
      f"（{es_dist}，含 pending 终点 {es_pending} 条，来源 event_assessments）")
    a(f"- 卡片: {rep.card_version_id or '无 active'}；信号 rule_version="
      f"{SIGNALS_RULE_VERSION}，config_hash={config_hash[:12]}…")
    if rep.reasons:
        a(f"- 异常/降级: {'；'.join(rep.reasons)}")
    else:
        a("- 异常: 无")
    a("")

    rep.markdown = "\n".join(L)
    rep.snapshot = {
        "gate": {"status": gate.status, "reason": gate.reason},
        "data_cutoff": cutoff,
        "card_version_id": rep.card_version_id,
        "week_end": week_end,
        "priority": rep.priority,
        "decision_points": [d[:120] for d in dp],
        "facts_states": {k: (v["state"] if v else None) for k, v in
                         {**facts, **weekly_facts}.items()},
        # r2 Phase 1/3：本股日历到期计数 + 消息面展示条数
        "calendar_due": len(cal_here),
        "message_shown": shown,
        "indicators_config_hash": indicators_config_hash,
    }
    return rep


# ---------------------------------------------------------------- report_runs（§6.1、§9.5）

def _next_revision(conn: sqlite3.Connection, report_type: str,
                   symbol: str | None, trade_date: str) -> int:
    r = conn.execute(
        "SELECT MAX(revision) AS r FROM report_runs "
        "WHERE report_type = ? AND trade_date = ? AND symbol IS ?",
        (report_type, trade_date, symbol),
    ).fetchone()
    return (r["r"] or 0) + 1


def _insert_report_run(conn: sqlite3.Connection, report_type: str,
                       symbol: str | None, trade_date: str, *,
                       revision: int, card_version_id: str | None,
                       config_hash: str, snapshot: dict, status: str,
                       file_path: str, run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO report_runs (report_type, symbol, as_of, trade_date, revision,
            card_version_id, rule_version, config_hash, input_snapshot_json,
            status, file_path, run_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (report_type, symbol, utc_now(), trade_date, revision, card_version_id,
         REPORT_RULE_VERSION, config_hash,
         json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
         status, file_path, run_id, utc_now()),
    )


# ---------------------------------------------------------------- 全池日报（§6.3）

PRIORITY_TITLES = {
    1: "优先级 1：数据异常（数据不完整或运行失败）",
    2: "优先级 2：证伪 / 复核逾期 / 换算 draft 待确认",
    3: "优先级 3：已确认决策点（档位 / 右侧 / 箱体）",
    4: "优先级 4：距触发边界 3% 以内观察点",
    5: "优先级 5：消息与普通状态更新",
}


def render_daily_report(reps: list[SymbolReport], skipped: list[tuple[str, str]],
                        trade_date: str, run_id: str, revision: int,
                        config_hash: str) -> str:
    L: list[str] = []
    a = L.append
    a(f"# 全池日报 — {trade_date}")
    a("")
    a("> 模板化生成（无 LLM 排序与摘要，§6.3/§6.4）；排序为确定性五级优先级，"
      "每条条目附排序原因。"
      + (f"revision {revision}（同日重跑生成新 revision 行并覆盖同名文件，§9.5）。"
         if revision > 1 else ""))
    a("")
    a(f"- run_id: {run_id}；rule_version: {REPORT_RULE_VERSION}；"
      f"config_hash: {config_hash[:12]}…；覆盖股票 {len(reps)} 只")
    a("")
    for prio in (1, 2, 3, 4, 5):
        group = [r for r in reps if r.priority == prio]
        # 同级：距边界百分比（升序，无则排后）→ symbol（§6.3）
        group.sort(key=lambda r: (r.sort_distance is None,
                                  r.sort_distance if r.sort_distance is not None else 0.0,
                                  r.symbol))
        a(f"## {PRIORITY_TITLES[prio]}")
        a("")
        if not group:
            a("（无）")
            a("")
            continue
        for i, r in enumerate(group, 1):
            a(f"### {i}. {r.symbol} — {r.headline}")
            a("")
            a(f"- 状态: {r.status}；卡片: {r.card_version_id or '无 active'}")
            for d in r.decision_points:
                a(f"- 决策点: {d}")
            a(f"- 排序原因: {r.sort_reason}")
            a(f"- 单股报告: reports/{r.symbol}/{trade_date}.md")
            a("")
    a("## 非交易日 / 跳过")
    a("")
    if skipped:
        for sym, why in skipped:
            a(f"- {sym}: {why}")
    else:
        a("（无）")
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------- 主流程

def run_reports(conn: sqlite3.Connection, trade_date: str,
                *, reports_root: Path | str | None = None,
                symbols: list[str] | None = None,
                run_id: str | None = None,
                config_hash: str | None = None,
                indicators_config_hash: str | None = None) -> ReportResult:
    """生成单股报告 + 全池日报（文件先写、report_runs 单一事务登记）。

    同日重跑：同名文件覆盖 + report_runs 新增 revision 行（§9.5 降级）。
    """
    date_type.fromisoformat(trade_date)
    root = Path(reports_root) if reports_root else REPORTS_ROOT
    run_id = run_id or f"report_{trade_date}"
    if config_hash is None:
        params, config_hash = load_params()
    else:
        params, _ = load_params()
    result = ReportResult(trade_date=trade_date, run_id=run_id)

    if symbols is None:
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM watchlist WHERE active = 1 ORDER BY symbol")]

    pending_inserts: list[tuple] = []
    reps: list[SymbolReport] = []
    for symbol in symbols:
        revision = _next_revision(conn, "single", symbol, trade_date)
        rep = build_symbol_report(
            conn, symbol, trade_date, params=params, config_hash=config_hash,
            indicators_config_hash=indicators_config_hash,
            run_id=run_id, revision=revision)
        if check_symbol_day(conn, symbol, trade_date).status == STATUS_NON_TRADING:
            result.skipped.append((symbol, "非交易日（门禁 non_trading_day），跳过单股报告"))
            continue
        path = root / symbol / f"{trade_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rep.markdown, encoding="utf-8")
        rep.file_path, rep.revision = str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path), revision
        reps.append(rep)
        pending_inserts.append(("single", symbol, revision, rep.card_version_id,
                                rep.snapshot, rep.status, rep.file_path))
        if revision > 1:
            result.notes.append(
                f"{symbol}: 同日重跑，report_runs 记 revision={revision}（§9.5）")
    result.symbols = reps

    # ---- 全池日报
    daily_revision = _next_revision(conn, "daily", None, trade_date)
    daily_md = render_daily_report(reps, result.skipped, trade_date, run_id,
                                   daily_revision, config_hash)
    daily_path = root / "daily" / f"{trade_date}.md"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(daily_md, encoding="utf-8")
    result.daily_path = str(daily_path.relative_to(ROOT) if daily_path.is_relative_to(ROOT) else daily_path)
    result.daily_revision = daily_revision
    overall = ("failed" if any(r.status == "incomplete" for r in reps) else
               "degraded" if any(r.status == "degraded" for r in reps) else "complete")
    daily_snapshot = {
        "symbols": {r.symbol: {"status": r.status, "priority": r.priority,
                               "card_version_id": r.card_version_id}
                    for r in reps},
        "skipped": result.skipped,
    }
    pending_inserts.append(("daily", None, daily_revision, None,
                            daily_snapshot, overall, result.daily_path))
    if daily_revision > 1:
        result.notes.append(
            f"全池日报: 同日重跑，report_runs 记 revision={daily_revision}（§9.5）")

    with conn:
        for report_type, symbol, revision, card_id, snapshot, status, fp in pending_inserts:
            _insert_report_run(
                conn, report_type, symbol, trade_date, revision=revision,
                card_version_id=card_id, config_hash=config_hash,
                snapshot=snapshot, status=status, file_path=fp, run_id=run_id)
    return result


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.report")
    parser.add_argument("--date", required=True, help="交易日 YYYY-MM-DD（市场本地）")
    parser.add_argument("--symbol", default=None, help="只生成单只股票（仍含全池日报）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--reports-root", default=None, help="报告根目录（默认 reports/）")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        res = run_reports(
            conn, args.date, reports_root=args.reports_root,
            symbols=[args.symbol] if args.symbol else None)
    finally:
        conn.close()
    print(res)
    return 0 if all(s.status != "incomplete" for s in res.symbols) else 2


if __name__ == "__main__":
    raise SystemExit(main())
