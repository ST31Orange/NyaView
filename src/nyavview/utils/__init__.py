"""Small generic helpers shared across engines and surfaces."""

from __future__ import annotations

import re

from nyavview.utils.process import OnLine
from nyavview.utils.process import RunResult
from nyavview.utils.process import run_process

__all__ = ["OnLine", "RunResult", "run_process", "safe_filename"]


def safe_filename(value: str) -> str:
    """Keep a filename usable on Windows while preserving most text."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned or "unnamed"
