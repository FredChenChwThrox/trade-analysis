"""三因子横截面评分（计划 §11：Momentum / Volatility / Liquidity）。

纯 pandas 向量化，在 akquant 引擎外预计算（计划 §10 因子与交易分离）。口径：
- momentum = 复权收盘 close.pct_change(mom_window)（20 日收益率）
- volatility = 日收益率 rolling(vol_window).std(ddof=0)（与 §4.1 BOLL 口径一致）
- liquidity = amount_raw rolling(liq_window).mean()；成交额**不做股份调整**（§4.1）
  amount 缺失为库内已知缺口 → 该股该日无分，剔除并出覆盖率，不用成交量冒充。

横截面处理链（每交易日）：三因子齐全的样本 → 两端 winsorize（分位裁剪）→ zscore
→ 权重加权（volatility 权重本身为负即方向统一）→ score。
截面内样本 < min_names 或 std≈0（无区分度）时该因子贡献中性 0 分并计数明示。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class FactorParams:
    momentum_window: int = 20
    volatility_window: int = 20
    liquidity_window: int = 20
    weights: dict[str, float] = field(default_factory=lambda: {
        "momentum": 0.5, "volatility": -0.3, "liquidity": 0.2})
    winsorize_pct: float = 0.05     # 每端裁剪分位
    min_names: int = 3              # 截面最少样本数，低于则因子贡献置 0


_FACTOR_COLS = ("momentum", "volatility", "liquidity")


def compute_symbol_factors(df: pd.DataFrame, p: FactorParams) -> pd.DataFrame:
    """单股 → 以 date 为索引的 (momentum/volatility/liquidity/amount_ok) 帧。

    df 需含列 date/close/amount_raw（close 为复权收盘；amount_raw 可全空）。
    """
    out = df[["date"]].copy()
    close = df["close"].astype(float)
    ret1d = close.pct_change()
    out["momentum"] = close.pct_change(p.momentum_window)
    out["volatility"] = ret1d.rolling(
        p.volatility_window, min_periods=p.volatility_window).std(ddof=0)
    amt = df["amount_raw"].astype(float)
    out["liquidity"] = amt.rolling(
        p.liquidity_window, min_periods=p.liquidity_window).mean()
    return out


def build_factor_table(per_symbol: dict[str, pd.DataFrame],
                       p: FactorParams) -> tuple[pd.DataFrame, dict]:
    """全部单股帧 → tidy 因子表 (date,symbol,m,v,l,score) + 覆盖率报告。

    行级：三因子当日齐备才进入横截面（缺额保留 NULL score 供覆盖率统计）。
    因子级：某日某因子截面样本 < min_names 或 std≈0（无区分度）→ 该因子
    当日贡献按中性 0 计并计数明示；若当日三因子全部无贡献则不产出任何
    score（整日无分，策略当周跳过）——不为可交易性制造伪区分度。
    """
    frames = []
    for sym, sdf in per_symbol.items():
        f = compute_symbol_factors(sdf, p)
        f["symbol"] = sym
        frames.append(f.reset_index(drop=True))
    # ignore_index 必须：各股帧索引各自从 0 起，重叠标签会令按日写入互相覆写
    long = pd.concat(frames, ignore_index=True)
    complete = long.dropna(subset=list(_FACTOR_COLS)).copy()

    stats = {"neutral_factor_dates": 0, "scored_rows": 0}
    score_series = pd.Series(np.nan, index=complete.index)
    for date, g in complete.groupby("date", sort=True):
        total = pd.Series(0.0, index=g.index)
        contributed = False
        for col in _FACTOR_COLS:
            w = p.weights[col]
            s = g[col]
            std = s.std(ddof=0)
            if len(s) < p.min_names or not np.isfinite(std) or std == 0:
                stats["neutral_factor_dates"] += 1
                continue
            lo, hi = s.quantile(p.winsorize_pct), s.quantile(1 - p.winsorize_pct)
            clipped = s.clip(lower=lo, upper=hi)
            total += w * ((clipped - clipped.mean()) / std)
            contributed = True
        if contributed:
            score_series.loc[g.index] = total

    complete["score"] = score_series
    tidy = long.merge(
        complete[["date", "symbol", "score"]], on=["date", "symbol"], how="left")

    scored = tidy.dropna(subset=["score"])
    per_sym = (scored.groupby("symbol").size()
               .reindex(sorted(per_symbol)).fillna(0).astype(int).to_dict())
    total_days = int(long["date"].nunique())
    stats.update({
        "scored_rows": int(len(scored)),
        "per_symbol_score_days": per_sym,
        "total_trade_days": total_days,
        "first_score_date": (str(scored["date"].min())
                             if len(scored) else None),
        "last_score_date": (str(scored["date"].max())
                            if len(scored) else None),
    })
    return tidy, stats


def build_score_map(tidy: pd.DataFrame) -> dict[str, dict[str, float]]:
    """tidy 表 → {date: {symbol: score}}（仅非空），日期升序，供策略 O(log n) 取用。"""
    sub = tidy.dropna(subset=["score"])
    out: dict[str, dict[str, float]] = {}
    for date, grp in sub.groupby("date"):
        out[str(date)] = dict(zip(grp["symbol"], grp["score"].astype(float)))
    return dict(sorted(out.items()))


def select_scores_asof(score_map: dict[str, dict[str, float]],
                       date: str) -> dict[str, float] | None:
    """取严格早于 date 的最近一天分数（T-1 收盘信号纪律）；无则 None。"""
    candidates = [d for d in score_map if d < date]
    if not candidates:
        return None
    return score_map[max(candidates)]
