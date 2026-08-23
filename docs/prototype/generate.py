"""单股页原型生成器：用库内真实数据填充 stock_page_template.html。

用法：
    uv run python docs/prototype/generate.py [symbol]   # 默认 603605.SH

输出 docs/prototype/stock_<code>.html，浏览器直接打开即可评审。
数据口径与 UI 一致：行情/卡片均不复权，价格刻度指标 ÷ 当日因子折回（§5.1/§5.4）。
资讯流为示例数据（真实资讯源二期接入，情感标签由 LLM 打标存档）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.ui.db import get_connection          # noqa: E402
from scripts.ui import queries                    # noqa: E402

TEMPLATE = Path(__file__).with_name("stock_page_template.html")
BAR_COUNT = 120
IND_FIELDS = ["ma5", "ma20", "ma60", "vol_mean20", "dif", "dea", "macd_hist"]

# 资讯流示例（仅供评审交互；真实数据二期接入，标签由 LLM 打标后存档）
NEWS_SAMPLE = [
    ("08-08", "pos", "2026 中报业绩快报：营收同比 +12.3%，归母净利同比 +9.8%", "公司公告"),
    ("08-05", "neu", "董事减持计划预披露：拟减持不超过 0.5% 股份", "公司公告"),
    ("07-30", "pos", "618 大促复盘：主品牌线上 GMV 同比 +18%，市占率提升", "行业资讯"),
    ("07-22", "neg", "化妆品新规征求意见：功效宣称备案趋严，或增加合规成本", "政策"),
    ("07-15", "neu", "券商研报：维持「买入」评级，目标价 68 元", "研报"),
]


def _f(v, nd=2):
    return None if v is None else round(float(v), nd)


def build_data(conn, symbol: str) -> dict:
    bars = queries.get_stock_bars(conn, symbol, "daily", price="unadjusted")[-BAR_COUNT:]
    ind_rows = queries.get_stock_indicators(
        conn, symbol, "daily", fields=IND_FIELDS, price="unadjusted")
    ind_by_date = {r["date"]: r for r in ind_rows}

    out_bars, out_ind = [], []
    for b in bars:
        out_bars.append({
            "trade_date": b["trade_date"],
            "open": _f(b["open"]), "high": _f(b["high"]),
            "low": _f(b["low"]), "close": _f(b["close"]),
            "volume": _f((b["volume"] or 0) / 10000, 1),
        })
        r = ind_by_date.get(b["trade_date"], {})
        out_ind.append({
            "ma5": _f(r.get("ma5")), "ma20": _f(r.get("ma20")), "ma60": _f(r.get("ma60")),
            "vol_mean20": _f((r.get("vol_mean20") or 0) / 10000, 1) if r.get("vol_mean20") else None,
            "dif": _f(r.get("dif"), 3), "dea": _f(r.get("dea"), 3),
            "macd_hist": _f(r.get("macd_hist"), 3),
        })

    card_row = conn.execute(
        "SELECT price_tiers_json, invalidation_json, swing_box_json, "
        "       right_side_trigger_json FROM strategy_card_versions "
        "WHERE symbol = ? AND status = 'active' LIMIT 1", (symbol,)).fetchone()
    card = None
    if card_row:
        tiers = json.loads(card_row["price_tiers_json"] or "{}").get("tiers", [])
        box = json.loads(card_row["swing_box_json"] or "{}")
        inv = json.loads(card_row["invalidation_json"] or "{}")
        rst = json.loads(card_row["right_side_trigger_json"] or "{}")
        card = {
            "tiers": [{"tier": t["tier"], "lo": float(t["zone_low"]),
                       "hi": float(t["zone_high"])} for t in tiers],
            "invalidation": float(inv["line"]) if inv.get("line") else None,
            "box": {"box_low": float(box["box_low"]) if box.get("box_low") else None,
                    "box_high": float(box["box_high"]) if box.get("box_high") else None},
            "right_side": {"trigger": float(rst["trigger_level"])
                           if rst.get("trigger_level") else None},
        }

    execs = queries.list_executions(conn, symbol=symbol)["items"]
    out_execs = [{"date": e["executed_at"][:10], "action": e["action_type"],
                  "price": _f(e["price"], 3)} for e in execs]

    first_date = out_bars[0]["trade_date"] if out_bars else "0000-01-01"
    sig_rows = conn.execute(
        "SELECT DISTINCT observed_on, signal FROM signal_facts "
        "WHERE symbol = ? AND triggered = 1 AND observed_on >= ? "
        "ORDER BY observed_on", (symbol, first_date)).fetchall()
    sig_marks = [{"date": r["observed_on"],
                  "name": queries.SIGNAL_NAMES.get(r["signal"], r["signal"])}
                 for r in sig_rows]

    return {"bars": out_bars, "indicators": out_ind, "card": card,
            "executions": out_execs, "sig_marks": sig_marks}


# ------------------------------------------------------------- HTML 片段

def _num_card(label, value, sub="", cls=""):
    return (f'<div class="num-card"><div class="label">{label}</div>'
            f'<div class="value {cls}">{value}</div><div class="sub">{sub}</div></div>')


def _pe_status_text(s: str | None) -> str:
    """pe_status 内部原因码 → 人读中文（valuation.py §原因码约定）。"""
    if not s:
        return ""
    if s.startswith("ok"):
        parts = ["正常"]
    elif "degraded" in s:
        parts = ["降级"]
    else:
        return "数据缺失"
    if "degraded_available_at" in s:
        parts.append("披露日降级")
    return "·".join(parts)


def num_cards_html(ov: dict, card: dict | None) -> str:
    cards = [_num_card("现价（不复权）",
                       f"{ov['close_raw']:.2f}" if ov["close_raw"] is not None else "—",
                       ov["latest_trade_date"] or "")]
    pct = ov["pct_chg"]
    cards.append(_num_card(
        "涨跌幅", f"{pct:+.2f}%" if pct is not None else "—", "",
        "up" if (pct or 0) >= 0 else "down"))
    cards.append(_num_card(
        "PE(TTM)", f"{ov['pe_ttm']:.1f}" if ov["pe_ttm"] is not None else "—",
        _pe_status_text(ov["pe_status"])))
    tier = ov.get("tier")
    if tier and tier.get("tier"):
        cards.append(_num_card("档位", f"T{tier['tier']} 内",
                               f"{float(tier['zone_low']):g}–{float(tier['zone_high']):g}"))
    elif tier:
        side = "上沿" if tier.get("nearest_side") == "high" else "下沿"
        cards.append(_num_card("档位", "档外",
                               f"距 T{tier.get('nearest_tier')} {side} {tier['dist_pct']:.1f}%"))
    else:
        cards.append(_num_card("档位", "—"))
    box = ov.get("box")
    box_sub = ""
    if card and card["box"]["box_low"] is not None:
        box_sub = f"{card['box']['box_low']:g}–{card['box']['box_high']:g}"
    cards.append(_num_card("箱体", box["text"] if box else "—", box_sub))
    rs = ov.get("right_side")
    rs_sub = ""
    if card and card["right_side"]["trigger"] is not None:
        rs_sub = f"触发 {card['right_side']['trigger']:g}"
    cards.append(_num_card("右侧", rs["text"] if rs else "—", rs_sub))
    acc = ov.get("accumulation")
    cards.append(_num_card("吸筹形态", acc["text"] if acc else "—"))
    ex = ov.get("exhaustion")
    cards.append(_num_card("衰竭信号",
                           f"{ex['active']}/{ex['total']} 活跃" if ex else "—",
                           f"完成周 {ex['week_end']}" if ex else ""))
    return "\n".join(cards)


def _state_cls(state: str | None) -> str:
    if state in ("active", "triggered", "confirmed"):
        return "st-on"
    if state in ("box_breached", "invalidated", "failed"):
        return "st-bad"
    if state in ("watching", "waiting_retest", "pending_signals", "consolidating"):
        return "st-warn"
    return "st-off"


def sig_rows_html(ov: dict) -> str:
    rows = []
    for s in ov["summaries"]:
        rows.append(
            f'<div class="sig-row"><span class="sig-name">{s["name"]}</span>'
            f'<span class="sig-state {_state_cls(s["state"])}">{s["state_text"]}</span>'
            f'<span class="sig-detail">{s["detail"]}</span></div>')
    return "\n".join(rows)


_NEWS_TAG = {"pos": ("正面", "ntag-pos"), "neg": ("负面", "ntag-neg"),
             "neu": ("无影响", "ntag-neu")}


def news_html() -> str:
    rows = []
    for date, tag, title, src in NEWS_SAMPLE:
        label, cls = _NEWS_TAG[tag]
        rows.append(
            f'<div class="news"><span class="d">{date}</span>'
            f'<span class="ntag {cls}">{label}</span>'
            f'<span class="flex-1">{title}</span>'
            f'<span class="src">{src}</span></div>')
    return "\n".join(rows)


def _kv(label, value):
    return (f'<div class="flex gap-2 py-0.5"><span class="flex-none w-20 text-gray-400'
            f' text-xs leading-5">{label}</span><span class="text-xs leading-5">{value}</span></div>')


def card_html(conn, symbol: str) -> str:
    row = conn.execute(
        "SELECT * FROM strategy_card_versions WHERE symbol = ? AND status = 'active' "
        "LIMIT 1", (symbol,)).fetchone()
    if row is None:
        return '<div class="text-sm text-gray-400">无 active 卡片</div>'
    c = queries._parse_card(dict(row))
    tiers = (c.get("price_tiers_json") or {}).get("tiers", [])
    box, inv = c.get("swing_box_json") or {}, c.get("invalidation_json") or {}
    rst = c.get("right_side_trigger_json") or {}
    eps = (c.get("earnings_scenarios_json") or {}).get("eps", {})
    val = c.get("valuation_scenarios_json") or {}
    pe = val.get("pe", {})
    scales = val.get("panic_floor_scales", [])
    win = val.get("sample_window", {})

    trs = "".join(
        f'<tr class="border-t border-gray-100"><td class="py-1">T{t["tier"]}</td>'
        f'<td class="font-mono">{t["zone_low"]}–{t["zone_high"]}</td>'
        f'<td class="text-xs text-gray-500">'
        f'{implied}</td></tr>'
        for t, implied in zip(tiers, _tier_implied(tiers, eps)))

    scales_txt = " → ".join(f'{s["date"]} PE {s["pe_ttm"]}' for s in scales)
    parts = [f"""
<div class="text-xs text-gray-500 mb-2">{c['card_version_id']} · {c['status']} ·
  {c.get('effective_from') or '—'} 生效 · 下次复核 {c.get('next_review_at') or '—'} · 口径不复权</div>

<div class="text-xs font-medium text-gray-600 mt-2 mb-1">三档价区（估值锚定）</div>
<table class="text-sm w-full"><thead><tr class="text-left text-gray-500 text-xs">
<th>档位</th><th>价区</th><th>反推口径</th></tr></thead><tbody>{trs}</tbody></table>

<div class="text-xs font-medium text-gray-600 mt-3 mb-1">情景假设</div>
{_kv('EPS 三情景', f"悲观 {eps.get('bear', '—')} / 中性 {eps.get('base', '—')} / 乐观 {eps.get('bull', '—')}")}
{_kv('PE 三情景', f"悲观 {pe.get('pessimistic', '—')} / 中性 {pe.get('neutral', '—')} / 乐观 {pe.get('optimistic', '—')}")}
{_kv('恐慌底刻度', scales_txt or '—')}
{_kv('样本窗口', f"{win.get('from', '—')} ~ {win.get('to', '—')}（{win.get('note', '—')}）")}
{_kv('体系判断', val.get('regime', '—'))}

<div class="text-xs font-medium text-gray-600 mt-3 mb-1">交易框架</div>
{_kv('证伪线', f"{inv.get('line', '—')} —— {inv.get('note', '')}")}
{_kv('波段箱体', f"{box.get('box_low', '—')}–{box.get('box_high', '—')}："
    f"买区 {box.get('buy_zone_low', '—')}–{box.get('buy_zone_high', '—')}，"
    f"卖区 {box.get('sell_zone_low', '—')}–{box.get('sell_zone_high', '—')}，"
    f"跌破 {box.get('box_invalidation', '—')} 箱体失效")}
{_kv('右侧确认', f"收盘站上 {rst.get('trigger_level', '—')} 触发，止损 {rst.get('stop_level', '—')}")}
"""]
    return "".join(parts)


def _tier_implied(tiers: list, eps: dict) -> list[str]:
    """每档价区反推隐含的 EPS×PE 口径（纯算术展示，便于核对估值锚）。"""
    out = []
    for t in tiers:
        lo, hi = float(t["zone_low"]), float(t["zone_high"])
        base = float(eps["base"]) if eps.get("base") else None
        bear = float(eps["bear"]) if eps.get("bear") else None
        if t["tier"] == 3 and bear:
            out.append(f"≈ 悲观EPS {bear:g} × PE {lo / bear:.1f}–{hi / bear:.1f}")
        elif base:
            out.append(f"≈ 中性EPS {base:g} × PE {lo / base:.1f}–{hi / base:.1f}")
        else:
            out.append("—")
    return out


def exec_html(conn, symbol: str) -> str:
    items = queries.list_executions(conn, symbol=symbol)["items"]
    if not items:
        return '<div class="text-sm text-gray-400">无执行记录</div>'
    trs = "".join(
        f'<tr><td class="font-mono text-xs">{e["executed_at"][:10]}</td>'
        f'<td class="{"down" if e["action_type"] == "sell" else "up"}">'
        f'{"卖出" if e["action_type"] == "sell" else "买入"}</td>'
        f'<td class="font-mono">{float(e["price"]):.3f}</td>'
        f'<td class="font-mono">{float(e["quantity"]):g}</td></tr>' for e in items)
    return ('<table class="text-sm w-full"><thead><tr class="text-left text-gray-500 '
            'text-xs"><th>日期</th><th>方向</th><th>价格</th><th>数量</th></tr></thead>'
            f'<tbody>{trs}</tbody></table>')


def options_html(conn, symbol: str) -> str:
    opts = []
    for w in sorted(queries.get_watchlist(conn),
                    key=lambda x: (x["market"], x["symbol"])):
        sel = " selected" if w["symbol"] == symbol else ""
        opts.append(f'<option value="{w["symbol"]}"{sel}>'
                    f'{w["name"]} {w["symbol"]}</option>')
    return "\n        ".join(opts)


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "603605.SH"
    conn = get_connection()
    ov = queries.get_stock_overview(conn, symbol, event_limit=15)
    if ov is None:
        raise SystemExit(f"watchlist 中无 {symbol}")
    data = build_data(conn, symbol)

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("<!--STOCK_OPTIONS-->", options_html(conn, symbol))
    html = html.replace("<!--CUTOFF-->", ov["latest_trade_date"] or "—")
    html = html.replace("<!--NUM_CARDS-->", num_cards_html(ov, data["card"]))
    html = html.replace("<!--SIG_ROWS-->", sig_rows_html(ov))
    html = html.replace("<!--NEWS_HTML-->", news_html())
    html = html.replace("<!--CARD_HTML-->", card_html(conn, symbol))
    html = html.replace("<!--EXEC_HTML-->", exec_html(conn, symbol))
    html = html.replace("/*__DATA__*/null",
                        json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    out = TEMPLATE.with_name(f"stock_{symbol.split('.')[0]}.html")
    out.write_text(html, encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
