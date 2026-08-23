"""信号层公共：配置加载、常量与周线数据结构（设计 §4.2、§5）。

- 参数一律取 config/signals.yaml 的 defaults（第一版不用 overrides），
  config_hash = 文件内容 sha256，随 signal_facts 入库（§4.2）。
- WeekBar 为周线信号计算的唯一输入结构（复权口径，来自 weekly_bars）。
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from scripts.adapters.common import sha256_file
from scripts.pipeline.db import CONFIG_DIR

RULE_VERSION = "signals_v1"
SIGNALS_CONFIG = CONFIG_DIR / "signals.yaml"

# 五项衰竭信号（设计 §5.3），signal_facts.signal 取值
WEEKLY_SIGNALS = ["panic", "dry_up", "no_new_low_3w", "divergence", "duration"]


def load_params() -> tuple[dict, str]:
    """读取 config/signals.yaml defaults 与内容哈希（§4.2）。"""
    doc = yaml.safe_load(SIGNALS_CONFIG.read_text(encoding="utf-8"))
    return doc["defaults"], sha256_file(SIGNALS_CONFIG)


@dataclass
class WeekBar:
    """完成周复权周线（weekly_bars 行映射）。"""

    week_end_date: str
    week_start_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
