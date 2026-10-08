from pathlib import Path

from nyavview.engines import FfmpegTranscoder
from nyavview.engines import YtDlpEngine
from nyavview.core.settings import Settings


async def download_url(
    settings: Settings,
    url: str,
    destination: Path,
    *,
    audio_only: bool,
    overwrite: bool,
) -> Path:
    return await YtDlpEngine(settings).download(
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
) -> Path:
    return await FfmpegTranscoder(settings).transcode(
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
) -> list[Path]:
    return await FfmpegTranscoder(settings).transcode_bilibili_cache(
        source,
        destination_dir,
        overwrite=overwrite,
    )
