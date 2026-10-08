from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field

ConfigPath = Optional[Path]


class Settings(BaseModel):
    ffmpeg_path: Path = Path("ffmpeg")
    download_dir: Path = Path("downloads")
    converted_dir: Path = Path("converted")
    cookie_file: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    audio_bitrate: str = "192k"
    video_preset: str = "medium"
    overwrite: bool = False
    max_concurrent_tasks: int = Field(default=2, ge=1, le=8)

    @classmethod
    def from_config(cls, config_path: ConfigPath = None) -> "Settings":
        """Load settings from an optional TOML file."""
        data: dict[str, object] = {}
        if config_path:
            import tomllib
            with Path(config_path).expanduser().open("rb") as handle:
                data = tomllib.load(handle)
        return cls.model_validate(data)
