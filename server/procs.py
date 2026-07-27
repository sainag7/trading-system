"""Subprocess helper that streams a child's output into the current job log.

Used by diagnostics and schedule shell-outs. Runs inside an ``aux`` job whose
contextvar stdout sink is active, so ``print``-ing each line routes it to that
job's live log (and the server console).
"""
from __future__ import annotations

import asyncio


async def run_streamed(argv: list[str], cwd: str | None = None,
                       stdin_text: str | None = None) -> int:
    """Run ``argv``, streaming combined stdout/stderr line-by-line to ``print``.
    Returns the exit code. Never raises for a non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    if stdin_text is not None and proc.stdin is not None:
        proc.stdin.write(stdin_text.encode())
        await proc.stdin.drain()
        proc.stdin.close()
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(line.decode(errors="replace").rstrip("\n"))
    return await proc.wait()
