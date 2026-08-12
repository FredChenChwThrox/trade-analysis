#!/usr/bin/env python3
"""Parse the embedded markdown table from a Gildata result CSV into a clean DataFrame.

Gildata APIs return a CSV whose `table_markdown` column embeds the real data
as a markdown table. Parsing it by hand is fiddly (literal \\n vs newlines,
column drift); this helper does it once.

Usage:
    python3 parse_gildata_table.py /path/to/gildata_result.csv --out /tmp/parsed.csv
    python3 parse_gildata_table.py /path/to/gildata_result.csv --cols 股票名称,区间涨跌幅（%）
"""
import argparse
import re
import sys

import pandas as pd


def parse_table(md: str) -> pd.DataFrame:
    lines = [l for l in re.split(r"\\n|\n", str(md)) if l.strip().startswith("|")]
    if len(lines) < 2:
        return pd.DataFrame()
    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    # 业绩预告等表格常有空表头/重复表头（如 "| 项目 | 本报告期 |  |  | 上年同期 |"），
    # 直接作列名会让 pd.concat 因重复索引抛 InvalidIndexError——去重命名（空名补序号，
    # 重名加 _2/_3 后缀），数据列位次保持不变。
    seen: dict[str, int] = {}
    uniq = []
    for i, h in enumerate(header):
        name = h or f"_col{i + 1}"
        seen[name] = seen.get(name, 0) + 1
        uniq.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    rows = []
    for l in lines[1:]:
        if re.match(r"^\|[\s\-:|]+\|$", l.strip()):
            continue
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if len(cells) == len(uniq):
            rows.append(cells)
    return pd.DataFrame(rows, columns=uniq)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--cols", default=None, help="comma-separated columns to keep")
    args = ap.parse_args()

    src = pd.read_csv(args.csv)
    if "table_markdown" not in src.columns:
        print("no table_markdown column; columns:", list(src.columns))
        return 1
    frames = []
    for _, r in src.iterrows():
        df = parse_table(r.get("table_markdown", ""))
        if not df.empty:
            frames.append(df)
    if not frames:
        print("no rows parsed")
        return 1
    out = pd.concat(frames, ignore_index=True)
    # 全行去重（gildata 常返回内容完全相同的重复 result 块）。
    # 不用首列去重：一致预期表同一股票多年度多行，首列去重会静默吞掉次年以后各行。
    out = out.drop_duplicates()
    if args.cols:
        keep = [c for c in args.cols.split(",") if c in out.columns]
        out = out[keep]
    for c in out.columns[1:]:
        try:
            out[c] = pd.to_numeric(out[c])
        except (ValueError, TypeError):
            pass
    print(out.to_string(index=False))
    if args.out:
        out.to_csv(args.out, index=False)
        print(f"[ saved -> {args.out} ]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
