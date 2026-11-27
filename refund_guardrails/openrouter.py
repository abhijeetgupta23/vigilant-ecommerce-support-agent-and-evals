"""Run the same bot on OpenRouter models (including the free ones).

OpenRouter speaks the OpenAI chat format. The bot speaks the Anthropic one, so this
client translates both ways and the bot, cases and scoring stay unchanged:

  - Anthropic tools            -> OpenAI "function" tools
  - assistant tool_use blocks  -> assistant message with tool_calls
  - user tool_result blocks    -> one "tool" message per result
  - the reply                  -> Anthropic-style content blocks (text / tool_use)

Any model id with a "/" in it (e.g. "google/gemma-4-31b-it:free") goes here.
Needs OPENROUTER_API_KEY, unless a proxy adds the key for you.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def is_openrouter(model: str) -> bool:
    return "/" in model


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["input_schema"]}} for t in tools]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                     for b in content if b.get("type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # user turn: tool results first (they must follow the assistant's tool_calls), then any text
        for b in content:
            if b.get("type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(b["content"])})
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        if text:
            out.append({"role": "user", "content": text})
    return out


def from_openai_message(msg: dict) -> list[dict]:
    blocks: list[dict] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for i, c in enumerate(msg.get("tool_calls") or []):
        fn = c.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}  # bad JSON from the model: the tool sees no arguments and reports an error
        if not isinstance(args, dict):
            args = {}
        blocks.append({"type": "tool_use", "id": c.get("id") or f"call_{i}",
                       "name": fn.get("name", ""), "input": args})
    return blocks


class OpenRouterLLM:
    def __init__(self, max_tokens: int = 1024, max_retries: int = 5, timeout: float = 120):
        self.timeout = timeout
        self.key = os.environ.get("OPENROUTER_API_KEY") or "proxy-injected"
        self.max_tokens = max_tokens
        self.max_retries = max_retries

    def create(self, *, model, system, messages, tools):
        body = {"model": model, "messages": to_openai_messages(system, messages),
                "tools": to_openai_tools(tools), "max_tokens": self.max_tokens}
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:  # honours HTTPS_PROXY
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read()[:300]
                if b"per-day" in detail or b"per day" in detail:  # daily free quota: retrying won't help
                    raise RuntimeError(f"OpenRouter daily limit reached: {detail!r}") from e
                if e.code in RETRY_STATUS and attempt < self.max_retries:
                    time.sleep(min(60, 2 ** attempt * 3))
                    continue
                raise RuntimeError(f"OpenRouter HTTP {e.code}: {detail!r}") from e
            if "error" in data:  # OpenRouter can return 200 with an error body
                raise RuntimeError(f"OpenRouter error: {data['error']}")
            return SimpleNamespace(content=from_openai_message(data["choices"][0]["message"]))
        raise RuntimeError("unreachable")
