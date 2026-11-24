"""Model adapters. The bot loop speaks one shape (Anthropic-style content blocks);
each adapter translates to and from its provider.

    anthropic:<model_id>       Anthropic Messages API       ANTHROPIC_API_KEY
    openai_compat:<model_id>   any OpenAI-compatible host   OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY

A bare model id means anthropic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

PROVIDERS = ("anthropic", "openai_compat")
DEFAULT_MAX_TOKENS = 1024


class ToolCallError(Exception):
    """The model produced a tool call whose arguments could not be parsed."""


@dataclass
class Response:
    content: list[dict]
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})


def parse_spec(spec: str) -> tuple[str, str]:
    """'provider:model_id' -> (provider, model_id). Model ids may contain colons."""
    head, sep, rest = spec.partition(":")
    if sep and head in PROVIDERS:
        if not rest:
            raise ValueError(f"No model id in {spec!r}")
        return head, rest
    return "anthropic", spec


def _to_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(obj)


# --- Anthropic -----------------------------------------------------------------

class AnthropicLLM:
    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS):
        import anthropic  # imported here so offline tests don't need it

        self.client = anthropic.Anthropic(max_retries=5)
        self.max_tokens = max_tokens

    def create(self, *, model, system, messages, tools):
        return self.client.messages.create(model=model, system=system, messages=messages,
                                           tools=tools, max_tokens=self.max_tokens)


# --- OpenAI-compatible -----------------------------------------------------------

def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t["input_schema"]}} for t in tools]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Anthropic-style history -> chat-completions messages."""
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
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # A user message made of blocks: tool results first, then any text.
        for b in content:
            if b.get("type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(b["content"])})
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        if text:
            out.append({"role": "user", "content": text})
    return out


def from_openai_message(message: dict) -> list[dict]:
    """One chat-completions assistant message -> Anthropic-style content blocks."""
    blocks: list[dict] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for i, call in enumerate(message.get("tool_calls") or []):
        call = _to_dict(call)
        fn = _to_dict(call.get("function") or {})
        name, raw = fn.get("name") or "", fn.get("arguments")
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw) if raw not in (None, "") else {}
            except (TypeError, ValueError) as exc:
                raise ToolCallError(f"{name}: arguments are not valid JSON: {str(raw)[:120]!r}") from exc
        if not isinstance(args, dict):
            raise ToolCallError(f"{name}: arguments are not a JSON object: {str(raw)[:120]!r}")
        if not name:
            raise ToolCallError("tool call has no function name")
        blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{i}", "name": name, "input": args})
    return blocks


class OpenAICompatLLM:
    """Any host that serves the OpenAI chat-completions API with tool calling."""

    def __init__(self, client: Any = None, max_tokens: int = DEFAULT_MAX_TOKENS):
        if client is None:
            base_url = os.environ.get("OPENAI_COMPAT_BASE_URL")
            api_key = os.environ.get("OPENAI_COMPAT_API_KEY")
            if not base_url or not api_key:
                raise SystemExit("Set OPENAI_COMPAT_BASE_URL and OPENAI_COMPAT_API_KEY first.")
            import openai  # imported here so offline tests don't need it

            client = openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=5)
        self.client = client
        self.max_tokens = max_tokens

    def create(self, *, model, system, messages, tools):
        resp = _to_dict(self.client.chat.completions.create(
            model=model, messages=to_openai_messages(system, messages),
            tools=to_openai_tools(tools), max_tokens=self.max_tokens))
        message = _to_dict(resp["choices"][0]["message"])
        usage = _to_dict(resp.get("usage") or {})
        return Response(content=from_openai_message(message),
                        usage={"input_tokens": usage.get("prompt_tokens") or 0,
                               "output_tokens": usage.get("completion_tokens") or 0})


def make_llm(provider: str, max_tokens: int = DEFAULT_MAX_TOKENS):
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("Set ANTHROPIC_API_KEY first.")
        return AnthropicLLM(max_tokens)
    if provider == "openai_compat":
        return OpenAICompatLLM(max_tokens=max_tokens)
    raise ValueError(f"Unknown provider {provider!r}")
