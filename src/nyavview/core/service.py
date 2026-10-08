"""Thin facade exposing download/convert operations to any frontend.

CLI, desktop UI, and future REST surfaces all call these functions; nothing
else needs to know about the engines. ``on_progress`` is an optional callback
receiving :class:`~nyavview.core.events.ProgressEvent` instances.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from nyavview.core.events import ProgressEvent
from nyavview.core.settings import Settings
from nyavview.engines import FfmpegTranscoder, YtDlpEngine

ProgressCallback = Callable[[ProgressEvent], None]


async def download_url(
    settings: Settings,
    url: str,
    destination: Path,
    *,
    audio_only: bool,
    overwrite: bool,
    on_progress: Optional[ProgressCallback] = None,
) -> Path:
    return await YtDlpEngine(settings, on_progress=on_progress).download(
        url,
        destination,
        audio_only=audio_only,
        overwrite=overwrite,
    )


async def transcode_local_file(
    settings: Settings,
    source: Path,
    destination_dir: Path,
    *,
    overwrite: bool,
    on_progress: Optional[ProgressCallback] = None,
) -> Path:
    return await FfmpegTranscoder(settings, on_progress=on_progress).transcode(
        source,
        destination_dir,
        overwrite=overwrite,
    )


async def transcode_bilibili_cache(
    settings: Settings,
    source: Path,
    destination_dir: Path,
    *,
    overwrite: bool,
    on_progress: Optional[ProgressCallback] = None,
) -> list[Path]:
    return await FfmpegTranscoder(settings, on_progress=on_progress).transcode_bilibili_cache(
        source,
        destination_dir,
        overwrite=overwrite,
    )
