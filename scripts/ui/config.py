"""UI 配置加载：config/ui.yaml（docs/ui_design_phase1.md §6）。"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UI_CONFIG = ROOT / "config" / "ui.yaml"


def load_ui_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_UI_CONFIG
    return yaml.safe_load(path.read_text(encoding="utf-8"))
