import asyncio
import sys
from pathlib import Path

import pytest

from nyavview.core.errors import ProcessTimeoutError
from nyavview.utils import run_process


def _py(args: list[str]) -> list[str]:
    return [sys.executable, "-c", "; ".join(args)]


async def test_streams_lines_to_callback() -> None:
    seen: list[tuple[str, str]] = []
    result = await run_process(
        _py(["print('hello')", "print('world')"]),
        on_line=lambda stream, line: seen.append((stream, line)),
    )
    assert result.ok
    assert "hello" in result.stdout and "world" in result.stdout
    assert any(line == "hello" for _, line in seen)


async def test_bad_encoding_does_not_raise() -> None:
    """GBK bytes on stdout must not raise UnicodeDecodeError (regression)."""
    code = "import sys; sys.stdout.buffer.write('中文测试'.encode('gbk'))"
    result = await run_process([sys.executable, "-c", code])
    assert result.returncode == 0
    assert len(result.stdout) >= 1  # decoded with errors="replace" instead of crashing


async def test_timeout_kills_process() -> None:
    code = "import time; time.sleep(30)"
    with pytest.raises(ProcessTimeoutError):
        await run_process([sys.executable, "-c", code], timeout=0.5)


async def test_async_callback_is_awaited() -> None:
    seen: list[str] = []

    async def on_line(stream: str, line: str) -> None:
        await asyncio.sleep(0)
        seen.append(line)

    result = await run_process(_py(["print('x')"]), on_line=on_line)
    assert result.ok
    assert "x" in seen
