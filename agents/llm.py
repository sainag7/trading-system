"""Shared LLM access for the agents, built on the **Claude Agent SDK**.

This is the single place that talks to a model. Every agent calls
:func:`generate_text` / :func:`generate_json`. Backends are tried in order:

  1. **Claude Agent SDK** (`claude_agent_sdk.query`) — the primary path. This is
     what also lets the decision/execution flow reach the Robinhood Trading MCP
     via ``mcp_servers``.
  2. **Anthropic Messages API** (`anthropic` SDK) — a lightweight fallback for
     the pure text-in/JSON-out agents when the Agent SDK runtime isn't present.
  3. **Offline** — if no backend/API key is available, returns ``None`` so the
     caller can fall back to a deterministic heuristic. This keeps **recommend
     mode fully runnable without any API keys**, which is how you should develop.

The functions never raise on backend/network failure; they return ``None`` and
let the agent degrade gracefully.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Default output ceiling for a single agent call. This is a CAP, not a spend —
# you are billed for tokens actually generated. It must be large enough for the
# biggest JSON an agent can legitimately return: too small and the response is
# silently truncated mid-object, `extract_json` returns None, and the caller
# degrades to its offline heuristic for what looks like no reason.
DEFAULT_MAX_TOKENS = 16000

# Cache backend availability so we only probe once per process.
_SDK_OK: bool | None = None
_ANTHROPIC_OK: bool | None = None

# Why the most recent backend call failed, so a caller that falls back to a
# deterministic heuristic can say WHY instead of failing silently.
_LAST_ERROR: str | None = None


def last_error() -> str | None:
    """Reason the most recent LLM call failed, or ``None`` if it succeeded."""
    return _LAST_ERROR


def _record_error(where: str, exc: Exception) -> None:
    """Remember and surface a backend failure. Never raises."""
    global _LAST_ERROR
    _LAST_ERROR = f"{where}: {type(exc).__name__}: {exc}"
    print(f"⚠️  LLM call failed ({_LAST_ERROR})", file=sys.stderr)


def load_prompt(name: str) -> str:
    """Load a system prompt markdown file from ``prompts/`` (without extension)."""
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text()


def _sdk_available() -> bool:
    global _SDK_OK
    if _SDK_OK is None:
        try:
            import claude_agent_sdk  # noqa: F401
            _SDK_OK = True
        except Exception:
            _SDK_OK = False
    return _SDK_OK


def _anthropic_available() -> bool:
    global _ANTHROPIC_OK
    if _ANTHROPIC_OK is None:
        try:
            import anthropic  # noqa: F401
            _ANTHROPIC_OK = bool(os.getenv("ANTHROPIC_API_KEY"))
        except Exception:
            _ANTHROPIC_OK = False
    return _ANTHROPIC_OK


def backend_name() -> str:
    # The four pure agents prefer the fast Messages API; the Agent SDK is only
    # needed for MCP (execution). Report the path the agents will actually use.
    if _anthropic_available():
        return "anthropic_api"
    if _sdk_available():
        return "claude_agent_sdk"
    return "offline"


async def generate_text(
    system_prompt: str,
    user_prompt: str,
    model: str,
    *,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
    max_turns: int = 1,
    setting_sources: list[str] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str | None:
    """Return the model's text output, or ``None`` if no backend is available.

    When ``mcp_servers`` is supplied the Agent SDK is used so the model can call
    those MCP tools. When ``setting_sources`` is supplied (e.g. ``["local"]``)
    the SDK **inherits Claude Code's configured MCP servers** — this is how the
    Robinhood Trading MCP is reached: its OAuth is held by Claude Code, so we let
    the SDK pick up the connected ``robinhood-trading`` server rather than passing
    a token. For tool-using flows pass a larger ``max_turns``.

    Speed: pure text-in/JSON-out agents (no MCP, no inheritance) use the
    lightweight Anthropic Messages API FIRST — far faster than the Agent SDK.
    """
    global _LAST_ERROR
    _LAST_ERROR = None
    force_sdk = mcp_servers is not None or setting_sources is not None
    # Pure agents: prefer the fast Messages API.
    if not force_sdk and _anthropic_available():
        text = await _via_anthropic(system_prompt, user_prompt, model,
                                    max_tokens=max_tokens)
        if text is not None:
            return text
    # MCP flows (or no API key) go through the Agent SDK.
    if _sdk_available():
        return await _via_agent_sdk(
            system_prompt, user_prompt, model,
            mcp_servers=mcp_servers, allowed_tools=allowed_tools, max_turns=max_turns,
            setting_sources=setting_sources,
        )
    # Last resort (e.g. MCP requested but SDK unavailable): try the API anyway.
    if not force_sdk and _anthropic_available():
        return await _via_anthropic(system_prompt, user_prompt, model,
                                    max_tokens=max_tokens)
    if _LAST_ERROR is None:
        _LAST_ERROR = "no LLM backend available (no ANTHROPIC_API_KEY and no Agent SDK)"
    return None


async def _via_agent_sdk(
    system_prompt: str, user_prompt: str, model: str, *,
    mcp_servers: dict | None, allowed_tools: list[str] | None, max_turns: int,
    setting_sources: list[str] | None = None,
) -> str | None:
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            max_turns=max_turns,
            allowed_tools=allowed_tools or [],
            mcp_servers=mcp_servers or {},
            # Inherit Claude Code's config (for the OAuth'd Robinhood MCP) ONLY when
            # asked; otherwise stay pure. Only be strict when we pass explicit servers.
            setting_sources=setting_sources if setting_sources is not None else [],
            strict_mcp_config=bool(mcp_servers),
            permission_mode="default",
        )
        chunks: list[str] = []
        final: str | None = None
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                final = message.result
        text = "".join(chunks).strip()
        return text or (final.strip() if final else None)
    except Exception as e:
        _record_error("claude_agent_sdk", e)
        return None


async def _via_anthropic(system_prompt: str, user_prompt: str, model: str, *,
                         max_tokens: int = DEFAULT_MAX_TOKENS) -> str | None:
    try:
        import anthropic

        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # A truncated response is unparseable JSON downstream, which used to
        # look like "no LLM backend". Name it explicitly instead.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            global _LAST_ERROR
            _LAST_ERROR = (f"anthropic_api: {model} hit max_tokens={max_tokens} "
                           f"and the response was truncated")
            print(f"⚠️  {_LAST_ERROR}", file=sys.stderr)
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
    except Exception as e:
        _record_error("anthropic_api", e)
        return None


def extract_json(text: str | None) -> Any | None:
    """Best-effort parse of a JSON object/array from model text.

    Handles ```json fenced blocks and surrounding prose. Returns ``None`` if no
    valid JSON can be recovered.
    """
    if not text:
        return None
    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1).strip() if fenced else text.strip()
    try:
        return json.loads(candidate)
    except Exception:
        pass
    # Fall back to the first {...} or [...] span.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except Exception:
                continue
    return None


async def generate_json(
    system_prompt: str,
    user_payload: Any,
    model: str,
    *,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
    max_turns: int = 1,
    setting_sources: list[str] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[Any | None, str | None]:
    """Generate and parse JSON. Returns ``(parsed_or_None, raw_text_or_None)``."""
    user_prompt = (
        user_payload if isinstance(user_payload, str)
        else json.dumps(user_payload, default=str, indent=2)
    )
    raw = await generate_text(
        system_prompt, user_prompt, model,
        mcp_servers=mcp_servers, allowed_tools=allowed_tools, max_turns=max_turns,
        setting_sources=setting_sources, max_tokens=max_tokens,
    )
    parsed = extract_json(raw)
    if parsed is None and raw:
        global _LAST_ERROR
        _LAST_ERROR = (_LAST_ERROR
                       or f"model returned text but no parseable JSON "
                          f"({len(raw)} chars)")
    return parsed, raw
