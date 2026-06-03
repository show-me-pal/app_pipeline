"""Thin Anthropic client wrapper.

Centralizes model choice, retries, and a `structured()` helper that forces the
model to return JSON matching a schema via tool-use (the reliable way to get
machine-parseable output from Claude).

NOTE ON MODELS: model strings change over time. Set ANTHROPIC_MODEL in your
environment. The current list lives at https://docs.claude.com/en/docs/about-claude/models
A Sonnet-class model is the right default here: extraction is high-volume and
doesn't need the most expensive model.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from anthropic import Anthropic, APIError, APIStatusError

# Sonnet-class default; override via env. Verify the live string in the docs.
DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


class LLMClient:
    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL,
                 max_retries: int = 4):
        # The SDK reads ANTHROPIC_API_KEY automatically if api_key is None.
        self.client = Anthropic(api_key=api_key) if api_key else Anthropic()
        self.model = model
        self.max_retries = max_retries

    def _with_retries(self, fn, *args, **kwargs):
        delay = 1.0
        last: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                return fn(*args, **kwargs)
            except APIStatusError as exc:
                # Retry only on rate-limit / overloaded / transient server errors.
                if exc.status_code in (429, 500, 503, 529):
                    last = exc
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
            except APIError as exc:
                last = exc
                time.sleep(delay)
                delay *= 2
        raise RuntimeError(f"LLM call failed after {self.max_retries} retries: {last}")

    def text(self, prompt: str, system: Optional[str] = None,
             max_tokens: int = 2000, temperature: float = 0.2) -> str:
        """Plain text completion."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = self._with_retries(self.client.messages.create, **kwargs)
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def structured(self, prompt: str, schema: dict, *, tool_name: str = "emit",
                   system: Optional[str] = None, max_tokens: int = 2000) -> dict:
        """Force JSON output that conforms to `schema` (a JSON Schema object).

        Uses a single tool with `tool_choice` forced, which is the most reliable
        way to get structured data back from Claude. Returns the parsed dict.
        """
        tool = {
            "name": tool_name,
            "description": "Return the extracted data in the required structure.",
            "input_schema": schema,
        }
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool_name},
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = self._with_retries(self.client.messages.create, **kwargs)
        for block in resp.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input  # already a dict
        # Fallback: try to parse any text block as JSON.
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Model did not return structured output: {exc}")
