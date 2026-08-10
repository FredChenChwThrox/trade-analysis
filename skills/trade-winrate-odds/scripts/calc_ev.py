#!/usr/bin/env python3
"""trade-winrate-odds: 六维加权评分 -> 方向/胜率/赔率/期望值/仓位 计算器

用法:
    python3 calc_ev.py '<json>'        # 从参数读
    echo '<json>' | python3 calc_ev.py # 从 stdin 读

输入 JSON:
{
  "horizon": "intraday|short|swing|long",   # 默认 swing
  "scores": {"technical": 2, "volume": 1, "news": 2,
             "macro": 0, "industry": 1, "position": null},  # null = 数据缺失
  "weights": {...},                          # 可选，整体覆盖 horizon 权重，和须为 1
  "direction": "long|short",                 # 可选；不传则从三价关系推断，无价格时按 z 建议方向
  "entry": 10.50, "stop": 9.80, "target": 12.00
}

输出 JSON: suggested_direction, z, win_rate(+区间), R, EV, grade, position_pct,
completeness 等。方向判定: z>=0.6 多头候选, z<=-0.6 空头候选, 其余观望。
零依赖，仅标准库。
"""
import json
import math
import sys

DIMS = ["technical", "volume", "news", "macro", "industry", "position"]

HORIZON_WEIGHTS = {
    "intraday": {"technical": 0.30, "volume": 0.25, "news": 0.20, "macro": 0.05, "industry": 0.10, "position": 0.10},
    "short":    {"technical": 0.28, "volume": 0.20, "news": 0.22, "macro": 0.08, "industry": 0.12, "position": 0.10},
    "swing":    {"technical": 0.25, "volume": 0.15, "news": 0.20, "macro": 0.15, "industry": 0.15, "position": 0.10},
    "long":     {"technical": 0.15, "volume": 0.05, "news": 0.15, "macro": 0.25, "industry": 0.20, "position": 0.20},
}

DIR_THRESHOLD = 0.6           # 方向判定阈值（对称）
P_FLOOR, P_CAP = 0.30, 0.72   # 胜率封底/封顶
LOGISTIC_K = 0.6
MAX_POSITION = 0.25           # 仓位上限


def fail(msg):
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def logistic(x):
    return 1.0 / (1.0 + math.exp(-LOGISTIC_K * x))


def clamp_p(p):
    return min(max(p, P_FLOOR), P_CAP)


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"JSON 解析失败: {e}")

    horizon = data.get("horizon", "swing")
    if horizon not in HORIZON_WEIGHTS:
        fail(f"horizon 须为 {list(HORIZON_WEIGHTS)} 之一")

    weights = data.get("weights") or HORIZON_WEIGHTS[horizon]
    if set(weights) != set(DIMS):
        fail(f"weights 必须恰好包含 {DIMS}")
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        fail("weights 之和必须为 1")

    scores_in = data.get("scores")
    if not isinstance(scores_in, dict):
        fail("缺少 scores 对象")

    # 校验并分离有效评分
    scores, missing = {}, []
    for d in DIMS:
        v = scores_in.get(d)
        if v is None:
            missing.append(d)
            continue
        if not isinstance(v, int) or not -3 <= v <= 3:
            fail(f"scores.{d} 须为 -3..+3 的整数或 null")
        scores[d] = v
    if not scores:
        fail("至少需要一个有效维度评分")

    # 缺失维度权重重归一化
    completeness = sum(weights[d] for d in scores)
    w = {d: weights[d] / completeness for d in scores}
    z = sum(w[d] * s for d, s in scores.items())

    # 方向判定（评分本身不带方向预设）
    if z >= DIR_THRESHOLD:
        suggested = "long"
    elif z <= -DIR_THRESHOLD:
        suggested = "short"
    else:
        suggested = "neutral"

    out = {
        "horizon": horizon,
        "z": round(z, 3),
        "suggested_direction": suggested,
        "completeness": round(completeness, 3),
        "missing_dims": missing,
    }
    if suggested != "neutral" and abs(abs(z) - DIR_THRESHOLD) <= 0.15:
        out["note"] = "|z| 接近阈值 0.6，方向信号偏弱，临近观望区"

    # 解析实际交易方向：显式 direction 优先，其次从三价关系推断
    direction = data.get("direction")
    if direction is not None and direction not in ("long", "short"):
        fail('direction 须为 "long" 或 "short"')

    entry, stop, target = data.get("entry"), data.get("stop"), data.get("target")
    has_prices = all(isinstance(x, (int, float)) for x in (entry, stop, target))

    if has_prices:
        if target > entry and stop < entry:
            inferred = "long"
        elif target < entry and stop > entry:
            inferred = "short"
        else:
            fail("三价关系无效：多头须 stop < entry < target；空头须 target < entry < stop")
        if direction is None:
            direction = inferred
        elif direction != inferred:
            fail(f"direction={direction} 与三价关系({inferred})矛盾")

    if suggested == "neutral":
        # 观望区：不计算 EV；有三价时仅给出赔率几何供参考
        out["note"] = "|z| < 0.6，多空信号混杂，建议观望；不计算 EV"
        if direction is not None:
            risk, reward = (entry - stop, target - entry) if direction == "long" else (stop - entry, entry - target)
            out["direction"] = direction
            out["odds_R"] = round(reward / risk, 2)
            out["note"] += f"；赔率 R={out['odds_R']} 仅供参考"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if direction is None:
        # 无价格、无显式方向：报告顺信号方向的胜率
        p = clamp_p(logistic(abs(z)))
        half = 0.05 + 0.10 * (1.0 - completeness)
        out.update({
            "win_rate_following_signal": round(p, 3),
            "win_rate_range": [round(max(p - half, 0.05), 3), round(min(p + half, 0.95), 3)],
        })
        out.setdefault("note", "未提供 entry/stop/target，仅输出胜率；先定义止损价再计算 EV")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 有确定方向：胜率相对该方向计算（顺信号用 |z|，逆信号自动变负）
    z_dir = z if direction == "long" else -z
    p = clamp_p(logistic(z_dir))
    half = 0.05 + 0.10 * (1.0 - completeness)
    out.update({
        "direction": direction,
        "win_rate": round(p, 3),
        "win_rate_range": [round(max(p - half, 0.05), 3), round(min(p + half, 0.95), 3)],
    })
    if (direction == "long" and z < 0) or (direction == "short" and z > 0):
        out["warning_direction_conflict"] = (
            f"交易方向({direction})与评分方向(z={z:.2f} 指向 {suggested})相反，"
            "胜率已按逆信号方向计算；报告中必须明示此冲突"
        )
    if direction == "short":
        out["note_short"] = "空头结论落地前确认该市场/账户支持做空（融券/期权）；A 股个股做空受限时，空头信号=持仓者减仓离场、空仓者观望"

    if not has_prices:
        out.setdefault("note", "未提供 entry/stop/target，仅输出方向与胜率；先定义止损价再计算 EV")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 赔率 / 期望值 / 仓位
    if direction == "long":
        risk, reward = entry - stop, target - entry
    else:
        risk, reward = stop - entry, entry - target
    r = reward / risk
    ev = p * r - (1.0 - p)
    kelly_q = (p - (1.0 - p) / r) / 4.0
    pos = min(max(kelly_q, 0.0), MAX_POSITION)
    if ev >= 0.30 and p >= 0.60 and completeness >= 0.999:
        grade = "A"
    elif ev >= 0.15:
        grade = "B"
    elif ev > 0:
        grade = "C"
    else:
        grade = "D"
    out.update({
        "entry": entry, "stop": stop, "target": target,
        "risk_per_share": round(risk, 4),
        "reward_per_share": round(reward, 4),
        "odds_R": round(r, 2),
        "EV": round(ev, 3),
        "grade": grade,
        "position_pct": round(pos * 100, 1),
        "note_kelly": "position_pct 为 1/4 Kelly 数学参考(上限25%)，非建议仓位",
    })
    if r < 1.5:
        out["warning"] = "R < 1.5，赔率不佳；不得为提高 R 而人为拉远目标价"

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
