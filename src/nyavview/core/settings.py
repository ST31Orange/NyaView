"""User settings, loaded from an optional TOML file.

The previous implementation only read TOML when an explicit ``--config`` path
was passed, and never resolved ffmpeg automatically. The new :meth:`discover`
looks for ``nyavview.toml`` in the current directory (as documented in the
README) with an explicit path taking priority; :meth:`resolve_ffmpeg` falls
back to the ffmpeg binary bundled with ``imageio-ffmpeg`` so a frozen EXE needs
no system ffmpeg install.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

CONFIG_FILENAME = "nyavview.toml"


class Settings(BaseModel):
    """Runtime settings for the download / transcode engines."""

    # pywebview bridge walks API attributes; keep Settings out of the bridge.
    _serializable = False

    ffmpeg_path: Optional[Path] = None  # None -> auto-resolve via imageio-ffmpeg
    download_dir: Path = Path("downloads")
    converted_dir: Path = Path("converted")
    cookie_file: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    audio_bitrate: str = "192k"
    video_preset: str = "medium"
    video_crf: int = Field(default=20, ge=0, le=51)
    overwrite: bool = False
    max_concurrent_tasks: int = Field(default=2, ge=1, le=8)

    @classmethod
    def discover(cls, explicit: Optional[Path] = None) -> "Settings":
        """Load settings from an explicit path, else ``nyavview.toml`` in CWD.

        An explicit path that does not exist raises :class:`ConfigurationError`;
        a missing default file simply returns defaults.
        """
        candidates: list[Path] = []
        if explicit is not None:
            candidates.append(Path(explicit).expanduser())
        candidates.append(Path.cwd() / CONFIG_FILENAME)

        for path in candidates:
            if path.is_file():
                with path.open("rb") as handle:
                    data = tomllib.load(handle)
                return cls.model_validate(data)

        if explicit is not None and not candidates[0].exists():
            from nyavview.core.errors import ConfigurationError

            raise ConfigurationError(f"config file not found: {candidates[0]}")
        return cls()

    def resolve_ffmpeg(self) -> str:
        """Return an ffmpeg executable path usable as the first argv item."""
        if self.ffmpeg_path:
            return str(Path(self.ffmpeg_path).expanduser().resolve())
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
