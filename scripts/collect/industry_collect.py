"""symbol_industry 采集器（消息面 r2 Phase 3）：东财全市场行业归属。

独立手触发/季度刷新（r2 §3.3：不进 daily stage 6）：
    uv run python -m scripts.collect.industry_collect --date 2026-08-28 --run-id run_ind

数据域：push2delay.eastmoney.com（与 push2 同路径 API 的延迟域——push2 被本网络
服务端拒绝，delay 域直连可用，2026-08-28 实测；板块归属为静态数据，延迟无碍）。
口径：东财行业板块现为新旧两套并存（fs=m:90+t:2 返回 496 个，含多级）；每股取
**包含它的最细板块**（东财个股归属口径，全市场唯一）；并列（旧Ⅱ级与新Ⅲ级成员
完全相同）取新分级 Ⅲ 命名。与 watchlist.industry_code（2026-08-28 回填）同口径。
落盘 industry/{date}/{run_id}/symbol_industry.csv → ingest 路由 ("akshare","industry")。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "data" / "raw" / "akshare"
BASE = "https://push2delay.eastmoney.com/api/qt/clist/get"
_UT = "bd1d9ddb04089700cf9c27f6f7426281"


def _fetch_page(session: requests.Session, fs: str, page: int) -> tuple[int, list]:
    r = session.get(BASE, params={
        "pn": page, "pz": 100, "po": 1, "np": 1, "ut": _UT, "fltt": 2,
        "invt": 2, "fid": "f3", "fs": fs, "fields": "f12,f14",
    }, timeout=15)
    r.raise_for_status()
    data = r.json()["data"]
    return data["total"], data["diff"]


def _fetch_all(session: requests.Session, fs: str) -> list[dict]:
    total, diff = _fetch_page(session, fs, 1)
    rows = list(diff)
    pages = -(-total // 100)
    for pg in range(2, pages + 1):
        _, diff = _fetch_page(session, fs, pg)
        rows.extend(diff)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.collect.industry_collect")
    parser.add_argument("--date", required=True, help="落盘目录日期 YYYY-MM-DD")
    parser.add_argument("--run-id", default="run_ind")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    session = requests.Session()
    session.trust_env = False  # 直连（push2delay 实测可达；系统代理会断连）
    boards = _fetch_all(session, "m:90 t:2".replace(" ", "+"))
    print(f"行业板块 {len(boards)} 个")

    best: dict[str, tuple[str, str, int]] = {}  # code -> (bk, name, size)
    for b in boards:
        bk, name = b["f12"], b["f14"]
        members = _fetch_all(session, f"b:{bk}")
        size = len(members)
        for m in members:
            code = str(m["f12"])
            cur = best.get(code)
            # 最细板块：成员最少者优先；并列取新分级（名称带Ⅲ）再按 BK 码确定性
            rank_new = 0 if name.endswith("Ⅲ") else 1
            if cur is None or (size, rank_new, bk) < (cur[2], 0 if cur[1].endswith("Ⅲ") else 1, cur[0]):
                best[code] = (bk, name, size)

    out = Path(args.out_root) / "industry" / args.date / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / "symbol_industry.csv"
    n = 0
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("symbol,industry_code,industry_name\n")
        for code in sorted(best):
            bk, name, _ = best[code]
            suffix = ".SH" if code.startswith(("6", "9")) else \
                (".BJ" if code.startswith(("4", "8")) else ".SZ")
            f.write(f"{code}{suffix},{bk},{name}\n")
            n += 1
    meta = {"run_id": args.run_id, "source": "akshare_em", "data_type": "industry",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "purpose": "symbol_industry 全市场行业归属（r2 Phase 3，季度刷新）",
            "requests": [{"api": "push2delay clist", "rows": n}]}
    (out / "_meta.json").write_text(
        __import__("json").dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[industry] {n} 只 → {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
