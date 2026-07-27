"""Config round-trip for the web app's Settings editor.

Generalises the dashboard's ``read_overrides``/``save_overrides`` pattern. Every
edit is written to ``config.local.yaml`` (deep-merged over ``config.yaml`` at load
time), so the commented base config is never rewritten. Secrets (``.env`` keys)
are never read or written here.

The ``dashboard`` block in the overlay holds web-app-only state that isn't part of
the trading config — currently ``hidden_deepdive_tickers`` (the reversible
"remove from deep dive" list).
"""
from __future__ import annotations

import copy

import yaml

from config import (
    DEFAULT_CONFIG_PATH, LOCAL_CONFIG_NAME, REPO_ROOT, VALID_MODES,
    _deep_merge, load_config,
)

LOCAL_CONFIG_PATH = REPO_ROOT / LOCAL_CONFIG_NAME

_HEADER = (
    "# Machine-managed by the trading-system web app (Settings + Deep dive).\n"
    "# Deep-merged over config.yaml at load time; delete this file to reset.\n"
)

# Sections a user may edit from the UI. Hard `risk` limits are included but the
# UI gates them behind an explicit confirmation. Secrets are intentionally absent.
EDITABLE_SECTIONS = (
    "risk", "strategy", "analysis", "discovery", "research", "recommend",
    "models", "data", "execution", "accounts", "notifications", "sectors",
)


def read_overrides() -> dict:
    """The current ``config.local.yaml`` overlay (``{}`` if none/corrupt)."""
    try:
        return yaml.safe_load(LOCAL_CONFIG_PATH.read_text()) or {}
    except Exception:
        return {}


def save_overrides(overrides: dict) -> None:
    LOCAL_CONFIG_PATH.write_text(_HEADER + yaml.safe_dump(overrides, sort_keys=False))


def reset_overrides() -> None:
    LOCAL_CONFIG_PATH.unlink(missing_ok=True)


# --- effective config view (for the editor) -------------------------------
def config_view() -> dict:
    """A JSON-serialisable snapshot of the EFFECTIVE config (base + overrides),
    grouped for the Settings UI, plus the raw overlay so the UI can show what is
    currently overridden. Never includes secrets."""
    cfg = load_config()
    raw = cfg.raw
    return {
        "general": {
            "mode": cfg.mode,
            "profile": cfg.profile,
            "profiles": list(cfg.valid_profiles()),
            "modes": list(VALID_MODES),
        },
        "universe": cfg.universe,
        "sectors": cfg.sectors,
        "risk": raw.get("risk", {}),
        "strategy": raw.get("strategy", {}),
        "analysis": raw.get("analysis", {}),
        "discovery": raw.get("discovery", {}),
        "research": raw.get("research", {}),
        "recommend": raw.get("recommend", {}),
        "models": raw.get("models", {}),
        "data": raw.get("data", {}),
        "execution": raw.get("execution", {}),
        "accounts": raw.get("accounts", {}),
        "notifications": raw.get("notifications", {}),
        "overrides": read_overrides(),
    }


def apply_overrides(patch: dict) -> dict:
    """Deep-merge ``patch`` into the current overlay and persist it, but only for
    recognised top-level keys (``mode``, ``profile``, ``universe`` and the
    :data:`EDITABLE_SECTIONS`). Validates by reloading; rolls back on any error.

    Returns ``{"ok": True}`` or ``{"ok": False, "error": ...}``.
    """
    allowed = {"mode", "profile", "universe", "dashboard", *EDITABLE_SECTIONS}
    clean = {k: v for k, v in (patch or {}).items() if k in allowed}
    if not clean:
        return {"ok": False, "error": "no editable keys in patch"}

    # Guard the two mode/profile scalars against invalid values before writing.
    if "mode" in clean and str(clean["mode"]).lower() not in VALID_MODES:
        return {"ok": False, "error": f"invalid mode {clean['mode']!r}"}

    current = read_overrides()
    merged = _deep_merge(current, clean)
    backup = copy.deepcopy(current)
    save_overrides(merged)
    try:
        load_config()  # will raise if the merged config is invalid
    except Exception as e:  # roll back a bad edit rather than break the system
        save_overrides(backup)
        return {"ok": False, "error": f"config rejected: {e}"}
    return {"ok": True}


# --- deep-dive hidden-ticker list (reversible "remove") -------------------
def get_hidden_tickers() -> list[str]:
    ov = read_overrides()
    lst = ((ov.get("dashboard") or {}).get("hidden_deepdive_tickers")) or []
    return [str(t).upper() for t in lst]


def hide_ticker(ticker: str) -> list[str]:
    t = str(ticker).strip().upper()
    hidden = set(get_hidden_tickers())
    if t:
        hidden.add(t)
    return _write_hidden(sorted(hidden))


def unhide_ticker(ticker: str) -> list[str]:
    t = str(ticker).strip().upper()
    hidden = [x for x in get_hidden_tickers() if x != t]
    return _write_hidden(hidden)


def _write_hidden(tickers: list[str]) -> list[str]:
    ov = read_overrides()
    dash = dict(ov.get("dashboard") or {})
    dash["hidden_deepdive_tickers"] = tickers
    ov["dashboard"] = dash
    save_overrides(ov)
    return tickers
