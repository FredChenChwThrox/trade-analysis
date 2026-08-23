#!/usr/bin/env python3
# Copyright © 2026 Fred Chen. All rights reserved.
# Project: fred-valuation-card-skill
# Created by: Fred Chen
# Date: 2026-07-19
# 

"""根据 EPS/PE 三情景生成估值情景矩阵与三档买入排期表（Markdown 输出）。

用法:
    python3 build_schedule.py --eps 3.9,3.5,3.2 --pe 18,15,12.5 --price 56.70
    python3 build_schedule.py --eps 3.9,3.5,3.2 --pe 18,15,12.5 --price 56.70 --winrate 50,60,65

参数约定:
    --eps     中性,悲观,极悲 三个 EPS 情景
    --pe      乐观,中性,悲观 三个 PE 情景
    --price   现价（可选，用于标注各档与现价的距离）
    --split   三档仓位比例，默认 30,35,35
    --winrate 各档胜率下沿（百分数，可选），取自 references/win-rate-scorecard.md
              的打分结果。提供后按 quarter-Kelly 输出各档仓位上限。
映射规则（默认值，可用参数覆盖）:
    第一档锚 = 中性EPS × 中性PE，区间 [锚×0.96, 锚]
    第二档锚 = 悲观EPS × 中性PE，区间 [锚×0.95, 锚]
    第三档锚 = 极悲EPS × 悲观PE，区间 [锚, 锚×1.08]
    证伪线   = 第三档锚（收盘有效跌破即冻结仓位）
Kelly 约定（仅 --winrate 提供时启用）:
    修复目标价 = 中性EPS × 乐观PE（假设：盈利不恶化 + 估值回到体系上沿）
    赔率 b = (目标价 − 买入价) / (买入价 − 证伪线)，买入价取触发区间中值
    f* = (p×b − q)/b，仓位上限 = f*/4（quarter-Kelly），p 取胜率下沿
    资金盘约定：把左侧仓总预算视为独立资金盘，f* 为该盘内的仓位上限。
    实际档位比例 = min(--split 固定比例, Kelly 上限)。Kelly 上限是封顶约束，
    不是目标仓位；为 0 表示该胜率下沿假设下不是正期望注。
"""
import argparse


def parse_triple(s, name):
    vals = [float(x) for x in s.split(",")]
    if len(vals) != 3:
        raise SystemExit(f"{name} 需要恰好 3 个逗号分隔的数字，收到: {s}")
    return vals


def fmt(x):
    return f"{x:.1f}"


def kelly_cap(pct, buy, target, falsify):
    """quarter-Kelly 仓位上限（%）与赔率 b。p 为百分数胜率。"""
    p = pct / 100.0
    q = 1 - p
    downside = buy - falsify
    if downside <= 0:
        return None  # 买入价在证伪线之下，赔率无意义
    b = (target - buy) / downside
    f = (p * b - q) / b
    return max(0.0, f / 4.0) * 100.0, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eps", required=True, help="中性,悲观,极悲 EPS，如 3.9,3.5,3.2")
    ap.add_argument("--pe", required=True, help="乐观,中性,悲观 PE，如 18,15,12.5")
    ap.add_argument("--price", type=float, default=None, help="现价")
    ap.add_argument("--split", default="30,35,35", help="三档仓位比例，如 30,35,35")
    ap.add_argument("--winrate", default=None,
                    help="各档胜率下沿（百分数），如 50,60,65，来自胜率打分卡")
    args = ap.parse_args()

    eps_n, eps_p, eps_w = parse_triple(args.eps, "--eps")
    pe_o, pe_n, pe_p = parse_triple(args.pe, "--pe")
    split = [int(x) for x in args.split.split(",")]
    if len(split) != 3 or sum(split) != 100:
        raise SystemExit("--split 需要 3 个合计为 100 的整数")
    winrates = None
    if args.winrate:
        winrates = parse_triple(args.winrate, "--winrate")
        if not all(0 < w < 100 for w in winrates):
            raise SystemExit("--winrate 需要 0–100 之间的百分数")

    eps_list = [("中性", eps_n), ("悲观", eps_p), ("极悲", eps_w)]
    pe_list = [("乐观", pe_o), ("中性", pe_n), ("悲观", pe_p)]

    print("## 情景矩阵（价格 = EPS × PE）\n")
    header = "| EPS \\\\ PE | " + " | ".join(f"{n} {v:g}" for n, v in pe_list) + " |"
    print(header)
    print("|" + "---|" * (len(pe_list) + 1))
    for en, ev in eps_list:
        row = f"| {en} {ev:g} | " + " | ".join(fmt(ev * pv) for _, pv in pe_list) + " |"
        print(row)

    t1 = eps_n * pe_n
    t2 = eps_p * pe_n
    t3 = eps_w * pe_p
    zones = [
        ("第一档", t1 * 0.96, t1, split[0], "估值到位即可买（允许买早）", "中性EPS × 中性PE"),
        ("第二档", t2 * 0.95, t2, split[1], "估值到位 且 衰竭信号 ≥2 项", "悲观EPS × 中性PE"),
        ("第三档", t3, t3 * 1.08, split[2], "估值到位 且 衰竭信号 ≥2 项", "极悲EPS × 悲观PE"),
    ]

    print("\n## 三档排期表\n")
    print("| | 触发价区 | 仓位 | 触发条件 | 矩阵出处 |")
    print("|---|---|---|---|---|")
    for name, lo, hi, pct, cond, src in zones:
        print(f"| {name} | {fmt(lo)}–{fmt(hi)} | {pct}% | {cond} | {src} |")

    print(f"\n证伪线：{fmt(t3)}（收盘有效跌破 = 极悲情景被击穿，冻结仓位，重做基本面判断，禁止继续摊低成本）")

    if winrates:
        target = eps_n * pe_o
        print("\n## 各档胜率与 Kelly 仓位上限\n")
        print(f"修复目标价假设：{fmt(target)}（中性EPS {eps_n:g} × 乐观PE {pe_o:g}，"
              f"即「盈利不恶化 + 估值回到体系上沿」；若基本面判断不支持该假设，本表不成立）")
        print("资金盘约定：左侧仓总预算视为独立资金盘，上限为盘内 quarter-Kelly。\n")
        print("| | 买入价中值 | 距证伪线 | 赔率 b | 胜率下沿 | Kelly 上限 | 固定比例 | 执行上限 |")
        print("|---|---|---|---|---|---|---|---|")
        for (name, lo, hi, pct, _, _), w in zip(zones, winrates):
            buy = (lo + hi) / 2
            res = kelly_cap(w, buy, target, t3)
            if res is None:
                print(f"| {name} | {fmt(buy)} | — | — | {w:g}% | 不适用 | {pct}% | {pct}% |")
                continue
            cap, b = res
            exec_cap = min(pct, cap)
            dist = (buy - t3) / buy * 100
            print(f"| {name} | {fmt(buy)} | {dist:.1f}% | {b:.2f} | {w:g}% "
                  f"| {cap:.1f}% | {pct}% | {exec_cap:.1f}% |")
        print("\n说明：Kelly 上限为 0 = 在胜率下沿假设下该档不是正期望注，若仍要执行，"
              "需胜率打分区间上沿支持并自行承担证据不足的风险；第三档赔率依赖证伪线的"
              "严格执行，跳空/滑点会压缩实际赔率，上限仅供参考。仓位递增的理由是证据"
              "变多，不是跌幅变大。")

    if args.price is not None:
        print(f"\n## 现价定位（现价 {args.price:g}）\n")
        for name, lo, hi, *_ in zones:
            if lo <= args.price <= hi:
                print(f"- 现价落在**{name}**区间（{fmt(lo)}–{fmt(hi)}）内")
            elif args.price > hi:
                pct = (args.price / hi - 1) * 100
                print(f"- 现价高于{name}上沿 {pct:.1f}%（{fmt(lo)}–{fmt(hi)}）")
            else:
                pct = (1 - args.price / lo) * 100
                print(f"- 现价低于{name}下沿 {pct:.1f}%（{fmt(lo)}–{fmt(hi)}）")


if __name__ == "__main__":
    main()
