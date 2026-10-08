from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from nyavview.core import service
from nyavview.core.errors import ConfigurationError, NyaViewError
from nyavview.core.settings import Settings

app = typer.Typer(help="NyaView video downloader and MP4 transcoder")
console = Console()


def _settings(config: Optional[Path]) -> Settings:
    """Load settings, translating discovery failures into CLI errors."""
    try:
        return Settings.discover(config)
    except ConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc


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
    settings = _settings(config)
    settings.cookie_file = cookie_file
    settings.cookies_from_browser = cookies_from_browser
    try:
        target = asyncio_run(
            service.download_url(
                settings,
                url,
                destination.expanduser().resolve(),
                audio_only=audio_only,
                overwrite=overwrite,
            )
        )
    except NyaViewError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]downloaded[/green] {target}")


@app.command()
def convert(
    source: Path = typer.Argument(..., help="Local file or Bilibili cache folder"),
    destination: Optional[Path] = typer.Option(Path("converted"), "--destination"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """Convert one local file or a Bilibili cache folder to MP4."""
    settings = _settings(None)
    target_dir = destination.expanduser().resolve()
    source_path = source.expanduser().resolve()
    try:
        if source_path.is_dir():
            outputs = asyncio_run(
                service.transcode_bilibili_cache(settings, source_path, target_dir, overwrite=overwrite)
            )
            for output in outputs:
                console.print(f"[green]converted[/green] {output}")
            return
        if not source_path.is_file():
            raise typer.BadParameter(f"source not a file or folder: {source_path}")
        target = asyncio_run(
            service.transcode_local_file(settings, source_path, target_dir, overwrite=overwrite)
        )
    except NyaViewError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]converted[/green] {target}")


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def run_app() -> None:
    app()
