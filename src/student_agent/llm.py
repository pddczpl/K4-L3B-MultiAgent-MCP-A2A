from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMSettings:
    """Configuration for an OpenAI-compatible local inference server."""

    base_url: str
    api_key: str
    primary_model: str
    verifier_model: str
    timeout_seconds: float

    @classmethod
    def load(cls) -> LLMSettings:
        return cls(
            base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", "ollama"),
            primary_model=os.getenv("LLM_PRIMARY_MODEL", "qwen3.5:9b"),
            verifier_model=os.getenv("LLM_VERIFIER_MODEL", "qwen3:8b"),
            timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
        )


class JSONLLM:
    """Small JSON-only wrapper that works with Ollama, vLLM and compatible APIs."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - exercised only in a broken install
            raise RuntimeError(
                'Missing LLM dependency. Run: python -m pip install -e ".[dev]"'
            ) from exc
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    async def generate(self, *, system: str, user: str, temperature: float = 0.0) -> dict[str, Any]:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
            seed=42,
        )
        content = response.choices[0].message.content or ""
        return _parse_json_object(content)


def build_llms() -> tuple[JSONLLM, JSONLLM]:
    settings = LLMSettings.load()
    common = {
        "base_url": settings.base_url,
        "api_key": settings.api_key,
        "timeout": settings.timeout_seconds,
    }
    return (
        JSONLLM(model=settings.primary_model, **common),
        JSONLLM(model=settings.verifier_model, **common),
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM did not return a JSON object") from None
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value
