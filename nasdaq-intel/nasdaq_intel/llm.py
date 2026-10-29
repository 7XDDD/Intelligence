"""LLM 客户端（OpenAI 兼容接口）。未配置 API Key 时自动降级为规则模式。

每次真实调用都会把「原始输入 + 原始输出 + 解析结果 + 耗时」写入
data/llm_logs/YYYY-MM-DD.jsonl，便于人工复核；日志不含 API Key。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, llm_cfg: dict[str, Any], log_dir: Path | None = None) -> None:
        self.cfg = llm_cfg
        self.base_url = str(llm_cfg.get("base_url", "")).rstrip("/")
        key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
        self.api_key = os.getenv(key_env, "").strip()
        self.model = llm_cfg.get("model", "gpt-4o-mini")
        self.timeout = int(llm_cfg.get("timeout", 90))
        self.temperature = float(llm_cfg.get("temperature", 0.2))
        self.max_tokens = int(llm_cfg.get("max_tokens", 1200))
        self.log_dir = Path(log_dir) if log_dir else None
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    # ---------- 对外接口 ----------
    def chat(self, system: str, user: str, *, label: str = "generic") -> str:
        if not self.available:
            raise LLMError("LLM 未配置（缺少 API Key）")
        request_id = uuid.uuid4().hex[:12]
        started = time.time()
        try:
            text, usage = self._complete(system, user)
        except LLMError as exc:
            self._write_log(request_id, label, system, user, None, None, None, started, "error", str(exc))
            raise
        self._write_log(request_id, label, system, user, text, None, usage, started, "ok", None)
        return text

    def chat_json(self, system: str, user: str, *, label: str = "generic") -> Any:
        if not self.available:
            raise LLMError("LLM 未配置（缺少 API Key）")
        request_id = uuid.uuid4().hex[:12]
        started = time.time()
        try:
            text, usage = self._complete(system, user)
        except LLMError as exc:
            self._write_log(request_id, label, system, user, None, None, None, started, "error", str(exc))
            raise
        parsed: Any = None
        error: str | None = None
        try:
            parsed = parse_json_loose(text)
        except LLMError as exc:
            error = str(exc)
        status = "parse_error" if error else "ok"
        self._write_log(request_id, label, system, user, text, parsed, usage, started, status, error)
        if error:
            self.last_error = error
            raise LLMError(error)
        return parsed

    # ---------- 内部 ----------
    def _complete(self, system: str, user: str) -> tuple[str, dict[str, Any] | None]:
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
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应格式异常：{data}") from exc
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        return text, usage

    def _write_log(
        self,
        request_id: str,
        label: str,
        system: str,
        user: str,
        response: str | None,
        parsed: Any,
        usage: dict[str, Any] | None,
        started: float,
        status: str,
        error: str | None,
    ) -> None:
        if not self.log_dir:
            return
        now = datetime.now(timezone.utc)
        entry = {
            "ts": now.isoformat(),
            "request_id": request_id,
            "label": label,
            "status": status,
            "model": self.model,
            "base_url": self.base_url,
            "duration_ms": int((time.time() - started) * 1000),
            "usage": usage,
            "system_sha1": hashlib.sha1(system.encode("utf-8")).hexdigest()[:12],
            "request": {"system": system, "user": user},
            "response": response,
            "parsed": parsed,
            "error": error,
        }
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"{now.date().isoformat()}.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # 日志失败不影响主流程
            self.last_error = f"LLM 日志写入失败：{exc}"


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
