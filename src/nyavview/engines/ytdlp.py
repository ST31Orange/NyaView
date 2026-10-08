from pathlib import Path
from typing import Awaitable, Callable

from nyavview.core.settings import Settings
from nyavview.utils import run_process


class YtDlpEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_args(
        self,
        url: str,
        destination: Path,
        *,
        audio_only: bool = False,
        overwrite: bool = False,
    ) -> list[str]:
        destination = destination.expanduser().resolve()
        template = str(destination / "%(title).180B.%(ext)s")
        args = [
            "yt-dlp",
            url,
            "--no-playlist",
            "--no-warnings",
            "--ignore-config",
            "--no-simulate",
            "--print-json",
            "--ffmpeg-location",
            str(self.settings.ffmpeg_path),
            "--output",
            template,
        ]
        if audio_only:
            args += ["--extract-audio", "--audio-format", "m4a"]
        else:
            args += [
                "--format",
                "bestvideo+bestaudio/best",
                "--merge-output-format",
                "mp4",
            ]
        if overwrite:
            args += ["--overwrite"]
        if self.settings.cookies_from_browser:
            args += ["--cookies-from-browser", self.settings.cookies_from_browser]
        elif self.settings.cookie_file:
            args += ["--cookies", str(self.settings.cookie_file.expanduser())]
        args += ["--concurrent-fragments", "4"]
        return args

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        audio_only: bool = False,
        overwrite: bool = False,
    ) -> Path:
        destination = destination.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        result = await run_process(
            self.build_args(url, destination, audio_only=audio_only, overwrite=overwrite)
        )
        if not result.ok:
            raise RuntimeError(result.stderr or "yt-dlp failed")
        return self._find_output(result.stdout, destination, audio_only)

    def _find_output(
        self,
        stdout: str,
        destination: Path,
        audio_only: bool,
    ) -> Path:
        try:
            lines = [line.strip() for line in stdout.splitlines() if line.strip()]
            info = json.loads(lines[-1])
            if filename := info.get("filename"):
                path = Path(filename)
                if path.exists():
                    return path.resolve()
        except (IndexError, json.JSONDecodeError):
            pass
        candidates = list(destination.glob("*.mp4" if not audio_only else "*.m4a"))
        if not candidates:
            raise FileNotFoundError("yt-dlp did not return an MP4/M4A")
        return max(candidates, key=lambda path: path.stat().st_mtime).resolve()
