#!/usr/bin/env python3
"""Verify announcement-search hits against strict keyword + growth criteria.

Gildata announcement semantic search is fuzzy: it returns companies whose
业绩预告/半年报 text only loosely matches. This script re-checks every hit
against exact regex keyword groups AND earnings-surge rules, and extracts the
YoY growth range from the original text.

Input:  one or more CSV files produced by gildata_query.py (announcement API).
Output: a ranked table of companies passing both gates, printed to stdout
        and saved as CSV.

Usage:
    python3 verify_announcements.py /tmp/ann_*.csv --min-growth 50 --out /tmp/verified.csv
"""
import argparse
import glob
import re
import sys

import pandas as pd

# Strong prosperity phrases (gate 2). Keep groups; report which ones hit.
KEYWORD_GROUPS = {
    "供不应求": r"供不应求|供应短缺|供给短缺|供货紧张|产能不足",
    "需求旺盛": r"需求旺盛|需求强劲|需求持续旺盛",
    "高景气": r"景气度|高景气|景气上行|景气度回升|景气度提升",
    "供给偏紧": r"供给偏紧|供应偏紧|供需偏紧|供应紧张|供给紧张|供应缺口|供需格局.{0,6}改善|供需格局.{0,6}优化",
    "价格上涨/中枢": r"价格中枢|价格上涨|价格上行|价格攀升|价格走高|提价|涨价|价格快速上涨|价格维持高位|量价齐升",
    "满产/满销": r"满产|满销|产能利用率.{0,4}(满|高)|产销两旺",
    "超预期": r"超预期",
    "订单充足": r"订单充足|订单饱满|订单充裕|订单充沛|在手订单|手持订单|订单.{0,6}大幅增长|订单放量",
}

# Exclusions: 预亏/预降 unless 扭亏为盈/预盈 is also stated.
PRE_KUI = r"预亏|业绩预降|首亏|续亏|扭盈为亏"
NIUKUI = r"扭亏为盈|实现扭亏|预盈"

GROWTH_PATTERNS = [
    r"(?:同比)?(?:增长|上升|增幅|增加|变动比例区间为?|上升幅度区间为?)[^\d]{0,12}([\d,.]+)%\s*[\-—–~至到]\s*([\d,.]+)%",
    r"增长\s*([\d,.]+)%\s*[\-—–~]\s*([\d,.]+)%",
    r"同比(?:增长|上升|增幅)[^\d]{0,12}([\d,.]+)%",
]


def normalize(s: str) -> str:
    for a, b in [("－", "-"), ("—", "-"), ("–", "-"), ("~", "-"), ("：", ":"), ("％", "%")]:
        s = s.replace(a, b)
    return s


def extract_growth(text: str):
    """Extract YoY net-profit growth. Search near 净利润 mentions first so that
    order/revenue growth figures (e.g. 订单金额同比+101%) are not mistaken for
    profit growth; fall back to a global match."""
    text = normalize(text)
    windows = [text[max(0, m.start() - 60):m.start() + 200]
               for m in re.finditer(r"净利润", text)] or [text]
    for win in windows:
        for pat in GROWTH_PATTERNS:
            m = re.search(pat, win)
            if m:
                lo = float(m.group(1).replace(",", ""))
                hi = float(m.group(2).replace(",", "")) if m.lastindex >= 2 else lo
                return lo, hi
    return None


def check_keywords(text: str):
    return [name for name, pat in KEYWORD_GROUPS.items() if re.search(pat, text)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="+", help="announcement CSVs (glob ok)")
    ap.add_argument("--min-growth", type=float, default=50.0,
                    help="min YoY growth lower-bound %% to count as 业绩大增 (default 50)")
    ap.add_argument("--out", default="/tmp/verified.csv")
    args = ap.parse_args()

    files = []
    for p in args.csvs:
        files.extend(glob.glob(p))
    if not files:
        print("no input csv matched")
        return 1

    records = {}
    for f in files:
        df = pd.read_csv(f)
        for _, row in df.iterrows():
            title = str(row.get("title", ""))
            m = re.match(r"([^:：]+)[:：]", title)
            comp = m.group(1).strip() if m else title
            records.setdefault(comp, []).append(str(row.get("table_markdown", "")))

    rows = []
    for comp, texts in records.items():
        full = " ".join(texts)
        kws = check_keywords(full)
        growth = extract_growth(full)
        pre_kui = bool(re.search(PRE_KUI, full)) and not re.search(NIUKUI, full)
        niuku = bool(re.search(NIUKUI, full))
        surge = (not pre_kui) and (niuku or (growth is not None and growth[0] >= args.min_growth))
        rows.append({
            "公司": comp,
            "关键词命中": ",".join(kws),
            "关键词数": len(kws),
            "增速下限%": growth[0] if growth else None,
            "增速上限%": growth[1] if growth else None,
            "扭亏": niuku,
            "预亏预降_剔除": pre_kui,
            "合格": surge and len(kws) > 0,
        })

    df = pd.DataFrame(rows).sort_values(["合格", "关键词数"], ascending=False)
    df.to_csv(args.out, index=False)
    ok = df[df["合格"]]
    print(f"候选 {len(df)} 家 -> 双因子合格 {len(ok)} 家 (min growth {args.min_growth}%)")
    for _, r in ok.iterrows():
        g = f"+{r['增速下限%']:.0f}%~{r['增速上限%']:.0f}%" if pd.notna(r["增速下限%"]) else ("扭亏" if r["扭亏"] else "?")
        print(f"  {r['公司']}: {g} | {r['关键词命中']}")
    excluded = df[(~df["合格"]) & (df["预亏预降_剔除"])]
    if len(excluded):
        print("已剔除(预亏/预降):", ", ".join(excluded["公司"].tolist()))
    print(f"[ saved -> {args.out} ]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
