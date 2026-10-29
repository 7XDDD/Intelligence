"""LLM 客户端（OpenAI 兼容接口）。未配置 API Key 时自动降级为规则模式。"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, llm_cfg: dict[str, Any]) -> None:
        self.cfg = llm_cfg
        self.base_url = str(llm_cfg.get("base_url", "")).rstrip("/")
        key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
        self.api_key = os.getenv(key_env, "").strip()
        self.model = llm_cfg.get("model", "gpt-4o-mini")
        self.timeout = int(llm_cfg.get("timeout", 90))
        self.temperature = float(llm_cfg.get("temperature", 0.2))
        self.max_tokens = int(llm_cfg.get("max_tokens", 1200))
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def chat(self, system: str, user: str) -> str:
        if not self.available:
            raise LLMError("LLM 未配置（缺少 API Key）")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.last_error = str(exc)
            raise LLMError(f"LLM 请求失败：{exc}") from exc
        if resp.status_code >= 400:
            self.last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            raise LLMError(self.last_error)
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM 响应格式异常：{data}") from exc

    def chat_json(self, system: str, user: str) -> Any:
        text = self.chat(system, user)
        return parse_json_loose(text)


def parse_json_loose(text: str) -> Any:
    """容忍模型输出前后的代码围栏或解释性文字。"""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    for start, end in (("{", "}"), ("[", "]")):
        s = cleaned.find(start)
        e = cleaned.rfind(end)
        if s != -1 and e > s:
            try:
                return json.loads(cleaned[s : e + 1])
            except ValueError:
                continue
    raise LLMError(f"无法解析模型返回的 JSON：{text[:200]}")
