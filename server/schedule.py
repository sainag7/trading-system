"""Manage the launchd daily-schedule from the web app (macOS).

Shells out to the existing ``scheduling/*.sh`` scripts so there is one source of
truth for the plist layout. The autonomous live job still requires the same
``ENABLE LIVE`` confirmation the CLI does — piped through only when the caller
explicitly asked to enable live trading.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SCHED_LOG = REPO_ROOT / "logs" / "scheduled.log"

LABEL_ADVICE = "com.trading-system.individual-advice"
LABEL_LIVE = "com.trading-system.agentic-live"

JOBS = [
    {"label": LABEL_ADVICE, "role": "individual", "mode": "recommend",
     "schedule": "Weekdays 10:00 local", "desc": "Daily advice (read-only — never trades)"},
    {"label": LABEL_LIVE, "role": "agentic", "mode": "live",
     "schedule": "Weekdays 10:05 local", "desc": "Autonomous live trading ($100 account)"},
]


def supported() -> bool:
    return sys.platform == "darwin"


def _installed(label: str) -> bool:
    return (AGENTS_DIR / f"{label}.plist").exists()


def status() -> dict:
    if not supported():
        return {"supported": False, "jobs": [], "log_tail": ""}
    jobs = [{**j, "installed": _installed(j["label"])} for j in JOBS]
    return {
        "supported": True,
        "jobs": jobs,
        "any_installed": any(j["installed"] for j in jobs),
        "log_tail": log_tail(),
    }


def log_tail(n: int = 60) -> str:
    try:
        return "\n".join(SCHED_LOG.read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def install(enable_live: bool = False) -> dict:
    if not supported():
        return {"ok": False, "error": "scheduling is macOS-only (launchd)"}
    argv = ["/bin/bash", str(REPO_ROOT / "scheduling" / "install.sh")]
    stdin = None
    if enable_live:
        argv.append("--enable-live")
        stdin = "ENABLE LIVE\n"   # the script's typed live confirmation
    proc = subprocess.run(argv, cwd=str(REPO_ROOT), input=stdin,
                          capture_output=True, text=True)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip(),
            **status()}


def uninstall() -> dict:
    if not supported():
        return {"ok": False, "error": "scheduling is macOS-only (launchd)"}
    proc = subprocess.run(["/bin/bash", str(REPO_ROOT / "scheduling" / "uninstall.sh")],
                          cwd=str(REPO_ROOT), capture_output=True, text=True)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip(),
            **status()}
