"""Direct client for the Robinhood Trading MCP — no model in the loop.

The Robinhood MCP is a standard MCP server over streamable HTTP with OAuth.
Until now every read (quotes, fundamentals, account) and every order went
through a Claude Code subprocess whose only job was to call one tool and echo
its JSON back — ~60k tokens of harness context, ~4 turns and ~30s per call,
and it was 85% of the system's LLM bill. This module calls the same tools
directly with the ``mcp`` client library: free, sub-second, deterministic.

Auth: the client holds its OWN OAuth session (dynamic client registration +
PKCE, same flow Claude Code uses), persisted to ``.cache/robinhood_oauth.json``
(gitignored). Run ``python -m data.robinhood_mcp login`` once; the provider
refreshes tokens after that. If ``ROBINHOOD_MCP_TOKEN`` is set, OAuth is
skipped and the bearer token is sent instead (headless/CI).

Usage::

    async with RobinhoodMCP(url) as rh:
        quotes = await rh.call("get_equity_quotes", {"symbols": ["SPY"]})

``call`` returns the tool's parsed JSON payload, or raises
:class:`RobinhoodMCPError` — never a model's paraphrase of it.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import webbrowser
from contextlib import asynccontextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_URL = "https://agent.robinhood.com/mcp/trading"
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".cache" / "robinhood_oauth.json"
# Loopback port for the one-time browser login. The redirect URI is part of
# the registered client, so it must be identical on every login.
CALLBACK_PORT = 8765


class RobinhoodMCPError(RuntimeError):
    """A tool call failed or returned something that is not a JSON payload."""


# ---------------------------------------------------------------------------
# OAuth persistence
# ---------------------------------------------------------------------------
class FileTokenStorage:
    """``mcp.client.auth.TokenStorage`` backed by one 0600 JSON file.

    Holds both the registered client (from dynamic client registration) and
    the current token set, so a login survives across runs and refreshes
    happen without a browser.
    """

    def __init__(self, path: Path = TOKEN_FILE):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, info) -> None:
        data = self._read()
        data["client_info"] = info.model_dump(mode="json", exclude_none=True)
        self._write(data)

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))


# ---------------------------------------------------------------------------
# One-shot loopback server for the authorization-code redirect
# ---------------------------------------------------------------------------
def _wait_for_callback(port: int, timeout: float = 300.0) -> tuple[str, str | None]:
    """Block until the browser redirects to ``localhost:port`` with a code."""
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            q = parse_qs(urlparse(self.path).query)
            result["code"] = (q.get("code") or [None])[0]
            result["state"] = (q.get("state") or [None])[0]
            result["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = ("Login failed: " + result["error"]) if result.get("error") \
                else "Robinhood connected. You can close this tab."
            self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *_):  # keep the terminal quiet
            pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.timeout = timeout
    srv.handle_request()          # exactly one request
    srv.server_close()
    if result.get("error") or not result.get("code"):
        raise RobinhoodMCPError(f"OAuth callback failed: {result.get('error') or 'no code'}")
    return result["code"], result.get("state")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class RobinhoodMCP:
    """Calls tools on the Robinhood Trading MCP server.

    Each :meth:`call` opens its own short-lived session (initialize → call →
    close). The streamable-HTTP transport is built on anyio task groups, which
    must be entered and left in the same task; a long-lived session shared
    across the provider's ``asyncio.gather``-ed research tasks would trip
    that. A handshake per call is a few hundred ms — the model-mediated path
    it replaces took ~30s.
    """

    def __init__(self, url: str | None = None, *, token: str | None = None,
                 storage: FileTokenStorage | None = None, interactive: bool = True,
                 audit=None):
        self.url = url or DEFAULT_URL
        self.token = token if token is not None else os.getenv("ROBINHOOD_MCP_TOKEN")
        self.storage = storage or FileTokenStorage()
        # A scheduled run must never sit waiting on a browser: if the stored
        # session is gone, fail fast with a hint instead.
        self.interactive = interactive
        self._audit = audit
        # Calls are serialised: the callers were sequential by design and the
        # OAuth token file must not be refreshed by two sessions at once.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "RobinhoodMCP":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def _auth(self):
        if self.token:
            return None
        if not self.interactive and not self.storage.has_tokens():
            raise RobinhoodMCPError(
                "no Robinhood OAuth session stored; run "
                "`python -m data.robinhood_mcp login` once from this repo")
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        async def redirect(url: str) -> None:
            print(f"Opening browser for Robinhood login…\n  {url}", file=sys.stderr)
            webbrowser.open(url)

        async def callback() -> tuple[str, str | None]:
            return await asyncio.to_thread(_wait_for_callback, CALLBACK_PORT)

        return OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata(
                client_name="trading-system",
                redirect_uris=[f"http://localhost:{CALLBACK_PORT}/callback"],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            storage=self.storage,
            redirect_handler=redirect if self.interactive else None,
            callback_handler=callback if self.interactive else None,
        )

    @asynccontextmanager
    async def _session(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        async with streamablehttp_client(
                self.url, headers=headers, auth=self._auth(),
                timeout=30, sse_read_timeout=120,
                terminate_on_close=False) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    # -- calls ----------------------------------------------------------------
    def _log(self, level: str, event: str, detail: Any) -> None:
        if self._audit:
            try:
                self._audit(level, event, detail)
            except Exception:
                pass

    async def list_tools(self) -> list[str]:
        async with self._lock, self._session() as session:
            res = await session.list_tools()
            return [t.name for t in res.tools]

    async def call(self, tool: str, args: dict | None = None,
                   timeout_s: float = 60.0) -> Any:
        """Call ``tool`` and return its parsed JSON payload (the ``data`` half
        of Robinhood's ``{"data": …, "guide": …}`` envelope).

        Raises :class:`RobinhoodMCPError` on transport failure or when the
        server flags the result as an error; never returns a paraphrase.
        """
        args = args or {}
        self._log("INFO", "mcp_request", {"tool": tool, "args": _redact(args)})
        try:
            async with self._lock, self._session() as session:
                res = await session.call_tool(
                    tool, args, read_timeout_seconds=timedelta(seconds=timeout_s))
        except RobinhoodMCPError:
            raise
        except Exception as e:
            self._log("ERROR", "mcp_transport_error", {"tool": tool, "error": str(e)})
            raise RobinhoodMCPError(f"{tool}: {type(e).__name__}: {e}") from e
        payload = _payload(res)
        if getattr(res, "isError", False):
            self._log("WARN", "mcp_tool_error", {"tool": tool, "payload": _clip(payload)})
            raise RobinhoodMCPError(f"{tool}: {_clip(payload)}")
        self._log("INFO", "mcp_response", {"tool": tool, "payload": _clip(payload)})
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload


def _payload(res) -> Any:
    sc = getattr(res, "structuredContent", None)
    if sc:
        return sc
    texts = [c.text for c in (res.content or []) if getattr(c, "type", "") == "text"]
    text = "\n".join(texts).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _clip(v: Any, n: int = 600) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _redact(args: dict) -> dict:
    return {k: ("***" if "token" in k.lower() else v) for k, v in args.items()}


# ---------------------------------------------------------------------------
# CLI: `python -m data.robinhood_mcp login|probe|call <tool> '<json>'`
# ---------------------------------------------------------------------------
async def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "probe"
    async with RobinhoodMCP(interactive=True) as rh:
        if cmd == "login":
            tools = await rh.list_tools()
            print(f"connected: {len(tools)} tools; session stored at {rh.storage.path}")
        elif cmd == "probe":
            tools = await rh.list_tools()
            print(f"{len(tools)} tools:", ", ".join(sorted(tools)))
            print(json.dumps(await rh.call("get_equity_quotes", {"symbols": ["SPY"]}),
                             indent=2, default=str)[:3000])
        elif cmd == "call" and len(argv) >= 2:
            args = json.loads(argv[2]) if len(argv) > 2 else {}
            print(json.dumps(await rh.call(argv[1], args), indent=2, default=str))
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
