"""Tests for the UI settings persistence (TOML round-trip)."""

from pathlib import Path

from nyavview.core.settings import Settings
from nyavview.ui.api import USER_CONFIG_PATH, load_user_settings, save_user_settings


def test_settings_round_trip_windows_paths(monkeypatch, tmp_path: Path) -> None:
    """Windows paths contain backslashes; they must survive save -> load."""
    config = tmp_path / ".nyavview.toml"
    monkeypatch.setattr("nyavview.ui.api.USER_CONFIG_PATH", config)

    settings = Settings(
        download_dir=Path("D:/我的视频/下载"),
        converted_dir=Path("C:\\out\\converted"),
        video_preset="fast",
        audio_bitrate="256k",
        max_concurrent_tasks=4,
    )
    save_user_settings(settings)

    # The written file must be valid TOML (backslashes escaped).
    text = config.read_text(encoding="utf-8")
    assert '"D:\\\\我的视频\\\\下载"' in text

    loaded = load_user_settings()  # must not raise TOMLDecodeError
    assert loaded.download_dir == Path("D:/我的视频/下载")
    assert loaded.converted_dir == Path("C:/out/converted")  # pathlib normalizes
    assert loaded.video_preset == "fast"
    assert loaded.audio_bitrate == "256k"
    assert loaded.max_concurrent_tasks == 4


def test_load_user_settings_falls_back_to_defaults(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / ".nyavview.toml"  # does not exist
    monkeypatch.setattr("nyavview.ui.api.USER_CONFIG_PATH", config)
    settings = load_user_settings()
    assert settings.download_dir == Path("downloads")
