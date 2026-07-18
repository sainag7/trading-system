"""Background scan runner for the dashboard.

Launches the orchestrator as a detached single-shot subprocess and tracks it via
a small state file, so status survives Streamlit reruns and even browser
refreshes. No Streamlit imports — pure, testable helpers.

SAFETY: the dashboard can only launch **recommend** scans. ``--mode recommend``
is hardcoded into the command below; there is deliberately no parameter that
could make the dashboard place trades. Preview/live remain CLI-only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / ".cache"
STATE_FILE = STATE_DIR / "dashboard_run.json"
LOG_FILE = STATE_DIR / "dashboard_run.log"

# Popen handle of a scan started by THIS process (e.g. the Streamlit server).
# Polling it both checks liveness and reaps the child — without this, a finished
# child lingers as a zombie and os.kill(pid, 0) keeps reporting it "alive".
_PROC: subprocess.Popen | None = None


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    # Same-process child: poll() is authoritative and reaps the zombie.
    if _PROC is not None and _PROC.pid == pid:
        return _PROC.poll() is None
    try:
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    # The pid exists, but it may be an unreaped zombie owned by another process
    # — treat 'Z' status (or a vanished entry) as finished.
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return bool(out) and not out.startswith("Z")
    except Exception:
        return True  # can't tell — assume alive rather than double-start


def job_label(status: dict) -> str:
    """Human label for the current/last job (used by the dashboard)."""
    if status.get("kind") == "explain":
        return f"deep research {status.get('ticker', '?')}"
    return f"{status.get('profile', '?')} scan"


def scan_status() -> dict:
    """Current job state: ``{"state": "idle"|"running"|"finished", ...}``."""
    st = _read_state()
    if not st.get("pid"):
        return {"state": "idle"}
    alive = _pid_alive(st["pid"])
    elapsed = max(0, int(time.time() - st.get("started_ts", time.time())))
    out = {
        "state": "running" if alive else "finished",
        "kind": st.get("kind", "scan"),
        "profile": st.get("profile"),
        "ticker": st.get("ticker"),
        "started": st.get("started", "?"),
        "elapsed_s": elapsed,
        "pid": st["pid"],
    }
    if not alive:
        tail = read_log(5)
        out["ok"] = ("Recommendation run complete" in tail) or ("Report saved" in tail) \
            or ("✅" in tail)
    return out


def _launch(argv: list[str], meta: dict) -> dict:
    """Spawn one background orchestrator job (single job at a time)."""
    global _PROC
    status = scan_status()
    if status["state"] == "running":
        return {"started": False,
                "reason": f"{job_label(status)} is already running — wait for it to finish"}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "w")  # noqa: SIM115 - handed to the subprocess
    proc = subprocess.Popen(
        argv, stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT),
        start_new_session=True,  # survives Streamlit reruns / server restarts
    )
    _PROC = proc
    STATE_FILE.write_text(json.dumps({
        "pid": proc.pid,
        **meta,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "started_ts": time.time(),
        "log": str(LOG_FILE),
    }))
    return {"started": True, "pid": proc.pid}


def start_scan(profile: str = "swing") -> dict:
    """Launch a recommend-mode scan in the background. Refuses to double-start."""
    # --mode recommend is HARDCODED: the dashboard can never launch a trading run.
    return _launch(
        [sys.executable, "-u", "orchestrator.py", "--mode", "recommend",
         "--profile", str(profile)],
        {"kind": "scan", "profile": str(profile)},
    )


def start_explain(ticker: str) -> dict:
    """Launch a single-ticker deep-research (explain) run in the background.

    Explain mode is READ-ONLY (zero trading side effects) — like the scan path,
    the mode is HARDCODED so the dashboard can never launch a trading run."""
    t = (ticker or "").strip().upper()
    if not t:
        return {"started": False, "reason": "enter a ticker symbol (e.g. NVDA)"}
    if not (t.isalpha() and 1 <= len(t) <= 5):
        return {"started": False,
                "reason": f"{ticker!r} doesn't look like a US equity symbol (1-5 letters)"}
    return _launch(
        [sys.executable, "-u", "orchestrator.py", "--mode", "explain", "--ticker", t],
        {"kind": "explain", "ticker": t},
    )


def read_log(n_lines: int = 40) -> str:
    """Tail of the current/last scan's log."""
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception:
        return ""


def clear_state() -> None:
    """Forget the last run (does not touch the log)."""
    try:
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
