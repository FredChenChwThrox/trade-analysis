"""LLM 配置与客户端（openai-compatible chat completions）。

config/llm.yaml：enabled=false 时调用方应跳过 LLM 阶段（设计关闭，非 degraded）。
api_key 从 api_key_env 指定的环境变量读取，不入库不入日志。

两条打标通道（产出同一 llm_v1 行、同一 schema、同一人审 gate）：
- API 通道：本模块 complete_json（daily 自动，需 api key）；
- agent/skill 通道：scripts/llm/inputs.py 导出底稿 → agent 打标 → import_tags 校验入库。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "llm.yaml"


class LLMDisabled(Exception):
    """LLM 未启用或缺少 api_key——调用方按设计跳过（非错误）。"""


class LLMError(Exception):
    """调用/解析失败（重试耗尽、HTTP 错误、JSON 非法）——调用方按 §2.5 丢弃该条。"""


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool
    base_url: str
    model: str
    api_key_env: str
    temperature: float
    timeout_seconds: int
    max_retries: int
    batch_size: int
    max_concurrency: int
    max_llm_calls_per_run: int
    review_gate: dict
    prompt_version: str

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


def load_config(path: Path | None = None) -> LLMConfig:
    doc = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8"))
    gate = doc.get("review_gate") or {}
    return LLMConfig(
        enabled=bool(doc.get("enabled")),
        base_url=doc["base_url"].rstrip("/"),
        model=doc["model"],
        api_key_env=doc.get("api_key_env", "LLM_API_KEY"),
        temperature=float(doc.get("temperature", 0.2)),
        timeout_seconds=int(doc.get("timeout_seconds", 30)),
        max_retries=int(doc.get("max_retries", 3)),
        batch_size=int(doc.get("batch_size", 20)),
        max_concurrency=int(doc.get("max_concurrency", 8)),
        max_llm_calls_per_run=int(doc.get("max_llm_calls_per_run", 1200)),
        review_gate=gate,
        prompt_version=str(doc.get("prompt_version", "llm_v1")),
    )


class LLMClient:
    """openai-compatible /chat/completions 薄封装：重试指数退避、严格 JSON 解析。

    self.last_content 保留最近一次原始返回文本（schema 拒绝时的诊断留痕用）。
    """

    def __init__(self, cfg: LLMConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.last_content: str | None = None

    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key())

    def complete_json(self, system: str, user: str) -> dict:
        """单次调用 → 解析为 dict；失败抛 LLMError（含重试耗尽）。"""
        if not self.available():
            raise LLMDisabled("llm disabled or api key missing")
        payload = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        headers = {"Authorization": f"Bearer {self.cfg.api_key()}"}
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                r = self.session.post(f"{self.cfg.base_url}/chat/completions",
                                      json=payload, headers=headers,
                                      timeout=self.cfg.timeout_seconds)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                self.last_content = content
                return _parse_json_content(content)
            except LLMError as exc:
                raise exc  # JSON 非法不重试（同输入同输出，重试无意义）
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"llm call failed after {self.cfg.max_retries} retries: {last}")


def _parse_json_content(content: str) -> dict:
    """剥掉可能的 ```json 围栏后解析；非法抛 LLMError（不冒充）。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise LLMError(f"llm output not json: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError("llm output is not a json object")
    return obj
