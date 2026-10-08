from pathlib import Path
import asyncio
from typing import Optional

import typer
from rich.console import Console

from nyavview.core.settings import Settings
from nyavview.core import service

app = typer.Typer(help="NyaView video downloader and MP4 transcoder")
console = Console()


@app.command()
def download(
    url: str = typer.Argument(..., help="Download URL"),
    destination: Optional[Path] = typer.Option(Path("downloads"), "--destination", help="Download folder"),
    audio_only: bool = typer.Option(False, "--audio-only", help="Download audio only"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing download"),
    cookie_file: Optional[Path] = typer.Option(None, "--cookie-file"),
    cookies_from_browser: Optional[str] = typer.Option(None, "--cookies-from-browser"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Download one URL into a folder."""
    settings = Settings.from_config(config)
    settings.download_dir = destination.expanduser().resolve()
    settings.cookie_file = cookie_file
    settings.cookies_from_browser = cookies_from_browser
    target = asyncio.run(
        service.download_url(settings, url, settings.download_dir, audio_only=audio_only, overwrite=overwrite)
    )
    console.print(f"[green]downloaded[/green] {target}")


@app.command()
def convert(
    source: Path = typer.Argument(..., help="Local file or Bilibili cache folder"),
    destination: Optional[Path] = typer.Option(Path("converted"), "--destination"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """Convert one local file or a Bilibili cache folder to MP4."""
    settings = Settings.from_config(None)
    target_dir = destination.expanduser().resolve()
    source_path = source.expanduser().resolve()
    if source_path.is_dir():
        outputs = asyncio.run(
            service.transcode_bilibili_cache(settings, source_path, target_dir, overwrite=overwrite)
        )
        for output in outputs:
            console.print(f"[green]converted[/green] {output}")
        return
    if not source_path.is_file():
        raise typer.BadParameter(f"source not a file or folder: {source_path}")
    target = asyncio.run(service.transcode_local_file(settings, source_path, target_dir, overwrite=overwrite))
    console.print(f"[green]converted[/green] {target}")


def run_app() -> None:
    app()
