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

import contextlib
import contextvars
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from agents import pricing

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


# ---------------------------------------------------------------------------
# Token / cost accounting
#
# Every LLM call in the system funnels through this module, so both backends are
# instrumented here and no call site has to change. Attribution (which agent,
# which run) rides on a contextvar rather than a parameter: the caller sets it
# once per phase and it propagates into the tasks `asyncio.gather` creates, so
# concurrently-researched tickers are each attributed correctly.
#
# The sink is a callback, so this module never imports storage — the same shape
# as the `audit=` callbacks the data providers take.
# ---------------------------------------------------------------------------
_usage_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "llm_usage_ctx", default={}
)
_usage_sink = None


def set_usage_sink(fn) -> None:
    """Register ``fn(record: dict)`` to receive one record per LLM call.

    Pass ``None`` to disable. The sink is called for FAILED calls too — a run
    that burns tokens and then errors still costs money, and leaving those out
    would under-report spend exactly where it matters most.
    """
    global _usage_sink
    _usage_sink = fn


@contextlib.contextmanager
def usage_context(**fields):
    """Tag every LLM call made inside this block (e.g. ``agent="research"``).

    Nests: inner fields are merged over the enclosing context.
    """
    token = _usage_ctx.set({**_usage_ctx.get(), **fields})
    try:
        yield
    finally:
        _usage_ctx.reset(token)


def _emit_usage(**record) -> None:
    """Hand one usage record to the sink. NEVER raises.

    Cost accounting is observability, not trading logic: a broken sink, a schema
    mismatch or a locked database must not take down a cycle.
    """
    if _usage_sink is None:
        return
    try:
        _usage_sink({**_usage_ctx.get(), **record})
    except Exception:
        pass


def _as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


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
        result: Any = None
        started = time.monotonic()
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                final = message.result
                result = message
        text = "".join(chunks).strip()
        out = text or (final.strip() if final else None)
        _emit_sdk_usage(result, model, started, ok=out is not None)
        return out
    except Exception as e:
        _record_error("claude_agent_sdk", e)
        # A failed tool-using call still consumed tokens (the max-turns failures
        # are the expensive ones), but the SDK raises before yielding a
        # ResultMessage, so only the attempt itself can be recorded.
        _emit_usage(model=model, backend="claude_agent_sdk", ok=0,
                    cost_source=pricing.UNPRICED, error=str(e)[:200])
        return None


def _emit_sdk_usage(result: Any, model: str, started: float, *, ok: bool) -> None:
    """Record one Agent SDK call from its ResultMessage.

    The SDK computes ``total_cost_usd`` itself, so that figure is preferred over
    our own pricing table; we only fall back to computing when it is absent.
    """
    if result is None:
        _emit_usage(model=model, backend="claude_agent_sdk", ok=1 if ok else 0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    cost_source=pricing.UNPRICED)
        return
    u = getattr(result, "usage", None) or {}
    in_tok = _as_int(u.get("input_tokens"))
    out_tok = _as_int(u.get("output_tokens"))
    cw = _as_int(u.get("cache_creation_input_tokens"))
    cr = _as_int(u.get("cache_read_input_tokens"))
    cost = getattr(result, "total_cost_usd", None)
    if cost is None:
        cost, source = pricing.cost_usd(model, in_tok, out_tok, cw, cr)
    else:
        source = pricing.SDK_REPORTED
    _emit_usage(
        model=model, backend="claude_agent_sdk",
        input_tokens=in_tok, output_tokens=out_tok,
        cache_write_tokens=cw, cache_read_tokens=cr,
        cost_usd=cost, cost_source=source,
        num_turns=_as_int(getattr(result, "num_turns", None)),
        duration_ms=_as_int(getattr(result, "duration_ms", None))
                    or int((time.monotonic() - started) * 1000),
        ok=0 if getattr(result, "is_error", False) or not ok else 1,
    )


async def _via_anthropic(system_prompt: str, user_prompt: str, model: str, *,
                         max_tokens: int = DEFAULT_MAX_TOKENS) -> str | None:
    started = time.monotonic()
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
        _emit_api_usage(resp, model, started)
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
    except Exception as e:
        _record_error("anthropic_api", e)
        # No usage is returned on a failed request — record the attempt so the
        # call count is honest, with cost unknown rather than zero.
        _emit_usage(model=model, backend="anthropic_api", ok=0,
                    cost_source=pricing.UNPRICED, error=str(e)[:200],
                    duration_ms=int((time.monotonic() - started) * 1000))
        return None


def _emit_api_usage(resp: Any, model: str, started: float) -> None:
    """Record one Messages API call from ``resp.usage``."""
    u = getattr(resp, "usage", None)
    in_tok = _as_int(getattr(u, "input_tokens", 0))
    out_tok = _as_int(getattr(u, "output_tokens", 0))
    cw = _as_int(getattr(u, "cache_creation_input_tokens", 0))
    cr = _as_int(getattr(u, "cache_read_input_tokens", 0))
    # Bill against the model the API says served the request, not the one we
    # asked for — they differ under a server-side fallback.
    served = getattr(resp, "model", None) or model
    cost, source = pricing.cost_usd(served, in_tok, out_tok, cw, cr)
    _emit_usage(
        model=served, backend="anthropic_api",
        input_tokens=in_tok, output_tokens=out_tok,
        cache_write_tokens=cw, cache_read_tokens=cr,
        cost_usd=cost, cost_source=source, num_turns=1,
        duration_ms=int((time.monotonic() - started) * 1000), ok=1,
    )


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
