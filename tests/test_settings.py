from pathlib import Path

import pytest

from nyavview.core.errors import ConfigurationError
from nyavview.core.settings import Settings


def test_discover_returns_defaults_when_no_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings.discover()
    assert settings.download_dir == Path("downloads")
    assert settings.ffmpeg_path is None


def test_discover_loads_explicit_config(tmp_path: Path) -> None:
    config = tmp_path / "custom.toml"
    config.write_text("audio_bitrate = '320k'\nmax_concurrent_tasks = 4\n", encoding="utf-8")
    settings = Settings.discover(config)
    assert settings.audio_bitrate == "320k"
    assert settings.max_concurrent_tasks == 4


def test_discover_reads_cwd_config(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "nyavview.toml").write_text("download_dir = 'out'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = Settings.discover()
    assert settings.download_dir == Path("out")


def test_discover_raises_on_missing_explicit(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        Settings.discover(tmp_path / "missing.toml")


def test_resolve_ffmpeg_uses_explicit_path(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg.exe"
    binary.write_bytes(b"MZ")
    settings = Settings(ffmpeg_path=binary)
    assert settings.resolve_ffmpeg().endswith("ffmpeg.exe")


def test_resolve_ffmpeg_falls_back(tmp_path: Path) -> None:
    """Without an explicit path it must resolve to a real bundled binary."""
    settings = Settings()
    resolved = settings.resolve_ffmpeg()
    assert resolved and "ffmpeg" in resolved.lower()
