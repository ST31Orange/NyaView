"""Progress event model shared by engines, service, and the desktop UI.

Engines emit :class:`ProgressEvent` instances through a callback; the queue
manager turns them into JSON payloads for the webview bridge. Keeping this in
``core`` lets the CLI ignore it and the UI consume it without touching engine
internals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ProgressEvent:
    kind: str  # "download" | "transcode"
    status: str  # "downloading" | "running" | "finished" | "error"
    progress: float | None = None  # 0..1 when known
    percent: float | None = None  # 0..100
    speed: str | None = None  # human-readable, e.g. "3.2MiB/s"
    eta: str | None = None
    detail: str | None = None  # human-readable line (title / output path)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
