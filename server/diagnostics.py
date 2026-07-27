"""In-dashboard diagnostics — run the offline self-check / test scripts as
subprocesses and stream their output to a job log. These are hermetic (no network,
no keys) and place nothing; they verify the pipeline and safety layers.
"""
from __future__ import annotations

import sys
from pathlib import Path

from server.procs import run_streamed

REPO_ROOT = Path(__file__).resolve().parent.parent

# name -> (human label, argv)
CHECKS: dict[str, tuple[str, list[str]]] = {
    "recommend": ("Recommend-mode check (zero trading side effects)",
                  [sys.executable, "recommend_check.py"]),
    "explain": ("Explain-mode check (grounded, side-effect-free)",
                [sys.executable, "explain_check.py"]),
    "accounts": ("Account routing + safety gates",
                 [sys.executable, "-m", "pytest", "test_accounts.py", "-q"]),
    "guardrails": ("Risk guardrails (59 tests)",
                   [sys.executable, "-m", "pytest", "risk/test_guardrails.py", "-q"]),
}


def names() -> list[dict]:
    return [{"name": k, "label": v[0]} for k, v in CHECKS.items()]


async def run(name: str) -> dict:
    entry = CHECKS.get(name)
    if entry is None:
        raise ValueError(f"unknown diagnostic {name!r}")
    label, argv = entry
    print(f"▶ {label}\n")
    code = await run_streamed(argv, cwd=str(REPO_ROOT))
    ok = code == 0
    print(f"\n{'✅ PASSED' if ok else f'❌ FAILED (exit {code})'}")
    return {"name": name, "ok": ok, "exit_code": code}
