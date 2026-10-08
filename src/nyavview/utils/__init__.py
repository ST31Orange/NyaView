from dataclasses import dataclass
import asyncio
import re
from pathlib import Path
from typing import Sequence


@dataclass
class RunResult:
    command: Sequence[str]
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_process(args: Sequence[str]) -> RunResult:
    """Run a command and capture stdin-free output."""
    strings = [str(value) for value in args]
    proc = await asyncio.create_subprocess_exec(
        *strings,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout, stderr = await proc.communicate()
    return RunResult(strings, stdout, stderr, proc.returncode or 0)


def safe_filename(value: str) -> str:
    """Keep a filename usable on Windows while preserving most text."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned or "unnamed"
