"""Streaming subprocess runner.

Replaces the previous all-in-memory ``communicate()`` wrapper:

- reads stdout/stderr line by line, so memory use stays bounded on long
  downloads / transcodes;
- decodes with ``errors="replace"`` so GBK/other console output on Chinese
  Windows can never raise ``UnicodeDecodeError``;
- exposes an optional ``on_line`` callback (sync or async) that lets engines
  and the desktop UI receive live progress without parsing afterwards;
- supports an optional timeout that kills the child process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from nyavview.core.errors import ProcessTimeoutError


@dataclass
class RunResult:
    command: Sequence[str]
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


OnLine = Callable[[str, str], "Awaitable[None] | None"]


async def run_process(
    args: Sequence[str],
    *,
    on_line: OnLine | None = None,
    timeout: float | None = None,
) -> RunResult:
    """Run a command, streaming stdout/stderr line by line.

    Args:
        args: Command and arguments; each item is converted with ``str()``.
        on_line: Optional callback ``(stream_name, line)`` invoked per line,
            where ``stream_name`` is ``"out"`` or ``"err"``. May be async.
        timeout: Optional wall-clock limit in seconds. On expiry the child is
            killed and :class:`ProcessTimeoutError` is raised.

    Returns:
        A :class:`RunResult` with the combined captured output.
    """
    strings = [str(value) for value in args]
    proc = await asyncio.create_subprocess_exec(
        *strings,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _drain(stream: asyncio.StreamReader, stream_name: str, sink: list[str]) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            sink.append(text)
            if on_line is not None:
                result = on_line(stream_name, text.rstrip("\r\n"))
                if asyncio.iscoroutine(result):
                    await result

    out_sink: list[str] = []
    err_sink: list[str] = []
    out_task = asyncio.ensure_future(_drain(proc.stdout, "out", out_sink))
    err_task = asyncio.ensure_future(_drain(proc.stderr, "err", err_sink))
    try:
        await asyncio.wait_for(asyncio.gather(out_task, err_task), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        # Let the readers hit EOF so no transport is left dangling on the loop.
        await asyncio.gather(out_task, err_task, return_exceptions=True)
        await proc.wait()
        raise ProcessTimeoutError(strings, timeout) from None
    except asyncio.CancelledError:
        # UI cancel path: kill the child so it cannot outlive the task.
        proc.kill()
        await asyncio.gather(out_task, err_task, return_exceptions=True)
        await proc.wait()
        raise
    returncode = await proc.wait()
    return RunResult(strings, "".join(out_sink), "".join(err_sink), returncode or 0)
