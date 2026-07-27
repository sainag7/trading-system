"""In-process async job manager with live log streaming.

Replaces the old detached-subprocess model in ``dashboard/runner.py``. Trading
(preview/live) needs structured control of the cycle and per-order approval, which
a subprocess reading ``input()`` from stdin cannot provide. So the orchestrator
runs **in this process** as an asyncio task, and its ``print()`` output is captured
per-job (task-scoped, via a contextvar) and streamed to the browser over SSE.

Two lanes:
  * **engine** jobs — one at a time (scan / research / plan / execute / live).
    They touch the single shared DB cycle, so overlapping them is disallowed.
  * **aux** jobs — diagnostics / schedule shell-outs. These run subprocesses and
    may overlap; they share the same log/stream plumbing.

Nothing here places an order. It only runs coroutines/subprocesses and captures
their output; all trading safety lives in ``Orchestrator``/``Executor``.
"""
from __future__ import annotations

import asyncio
import contextvars
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# Task-scoped sink: when set, ``print()`` output from the current task (and the
# child tasks/threads that copy its context) is appended here instead of only
# hitting the real console.
_sink: contextvars.ContextVar["Job | None"] = contextvars.ContextVar("job_sink", default=None)


class _StdoutRouter:
    """A ``sys.stdout`` proxy that tees writes to the active job's log (if any)
    AND to the real stdout, so the server console still shows progress."""

    def __init__(self, real):
        self._real = real

    def write(self, s: str) -> int:
        job = _sink.get()
        if job is not None and s:
            job.append(s)
        return self._real.write(s)

    def flush(self) -> None:
        self._real.flush()

    # Streamlit/uvicorn occasionally probe these.
    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


_installed = False


def install_stdout_router() -> None:
    """Install the tee once, at server startup. Idempotent."""
    global _installed
    if not _installed:
        sys.stdout = _StdoutRouter(sys.stdout)  # type: ignore[assignment]
        _installed = True


class EngineBusy(RuntimeError):
    """Raised when an engine job is requested while another is running."""


@dataclass
class Job:
    id: str
    kind: str            # scan | research | plan | execute | live | diagnostic | schedule
    label: str
    lane: str = "engine"        # engine | aux
    status: str = "running"     # running | done | error
    started_ts: float = field(default_factory=time.time)
    finished_ts: float | None = None
    lines: list[str] = field(default_factory=list)   # accumulated log chunks
    result: dict | None = None
    error: str | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)

    def append(self, chunk: str) -> None:
        self.lines.append(chunk)
        # Wake any SSE subscribers.
        self._event.set()

    def _touch(self) -> None:
        self._event.set()

    @property
    def log(self) -> str:
        return "".join(self.lines)

    @property
    def elapsed_s(self) -> int:
        end = self.finished_ts or time.time()
        return max(0, int(end - self.started_ts))

    def status_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "lane": self.lane,
            "label": self.label,
            "status": self.status,
            "elapsed_s": self.elapsed_s,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._engine_job_id: str | None = None

    # -- introspection -----------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def engine_job(self) -> Job | None:
        return self._jobs.get(self._engine_job_id) if self._engine_job_id else None

    def engine_busy(self) -> bool:
        j = self.engine_job()
        return j is not None and j.status == "running"

    # -- running -----------------------------------------------------------
    def start_engine(self, kind: str, label: str,
                     factory: Callable[[Job], Awaitable[dict | None]]) -> Job:
        """Start a single-at-a-time in-process engine job. ``factory`` is an async
        callable that receives the Job and returns a result dict; its ``print``
        output is captured to the job log. Raises :class:`EngineBusy` if one runs."""
        if self.engine_busy():
            busy = self.engine_job()
            raise EngineBusy(f"{busy.label} is already running — wait for it to finish")
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label, lane="engine")
        self._jobs[job.id] = job
        self._engine_job_id = job.id
        job.task = asyncio.create_task(self._run(job, factory))
        return job

    def start_aux(self, kind: str, label: str,
                  factory: Callable[[Job], Awaitable[dict | None]]) -> Job:
        """Start an auxiliary job (diagnostics/schedule). May overlap engine jobs."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label, lane="aux")
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, factory))
        return job

    async def _run(self, job: Job, factory: Callable[[Job], Awaitable[dict | None]]) -> None:
        token = _sink.set(job)   # capture this task's print() output into the job
        try:
            job.result = await factory(job)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "error"
            job.error = "cancelled"
            job.append("\n⏹  Job cancelled.\n")
            raise
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            job.status = "error"
            job.error = str(e)
            job.append(f"\n❌ {type(e).__name__}: {e}\n")
        finally:
            _sink.reset(token)
            job.finished_ts = time.time()
            if self._engine_job_id == job.id:
                self._engine_job_id = None
            job._touch()

    # -- SSE streaming -----------------------------------------------------
    async def stream(self, job_id: str):
        """Async generator of SSE events for a job: incremental log chunks plus a
        terminal status event. Safe to attach mid-run or after completion."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        idx = 0
        while True:
            # Flush any log accumulated since we last yielded.
            while idx < len(job.lines):
                yield {"event": "log", "data": job.lines[idx]}
                idx += 1
            if job.status != "running":
                yield {"event": "status", "data": job.status}
                return
            job._event.clear()
            try:
                await asyncio.wait_for(job._event.wait(), timeout=15)
            except asyncio.TimeoutError:
                # Heartbeat so proxies/browsers keep the connection open.
                yield {"event": "ping", "data": str(job.elapsed_s)}


# Process-wide singleton.
manager = JobManager()
