from pathlib import Path

from nyavview.core.errors import DownloadError
from nyavview.core.settings import Settings
from nyavview.engines.ytdlp import YtDlpEngine, _human_bytes


def _engine() -> YtDlpEngine:
    return YtDlpEngine(Settings())


def test_human_bytes() -> None:
    assert _human_bytes(None) is None
    assert _human_bytes(0) is None
    assert _human_bytes(512) == "512.0B"
    assert _human_bytes(2048) == "2.0KiB"
    assert _human_bytes(5 * 1024 * 1024) == "5.0MiB"


def test_opts_audio_only() -> None:
    opts = _engine()._opts(Path("d"), audio_only=True, overwrite=False)
    assert opts["format"] == "bestaudio/best"
    assert opts["postprocessors"][0]["preferredcodec"] == "m4a"
    assert "merge_output_format" not in opts


def test_opts_video() -> None:
    opts = _engine()._opts(Path("d"), audio_only=False, overwrite=False)
    assert opts["format"] == "bestvideo+bestaudio/best"
    assert opts["merge_output_format"] == "mp4"
    assert "postprocessors" not in opts


def test_opts_cookie_precedence(tmp_path: Path) -> None:
    engine = YtDlpEngine(Settings(cookies_from_browser="edge", cookie_file=tmp_path / "c.txt"))
    opts = engine._opts(Path("d"), audio_only=False, overwrite=False)
    assert opts["cookiesfrombrowser"] == ("edge",)
    assert "cookiefile" not in opts


def test_opts_cookie_file(tmp_path: Path) -> None:
    engine = YtDlpEngine(Settings(cookie_file=tmp_path / "c.txt"))
    opts = engine._opts(Path("d"), audio_only=False, overwrite=False)
    assert opts["cookiefile"] == str((tmp_path / "c.txt").expanduser())


def test_opts_overwrite_flag() -> None:
    opts = _engine()._opts(Path("d"), audio_only=False, overwrite=True)
    assert opts["overwrites"] is True


def test_locate_output_uses_report(tmp_path: Path) -> None:
    f = tmp_path / "real.mp4"
    f.write_bytes(b"x")
    info = {"requested_downloads": [{"filepath": str(f)}]}
    assert YtDlpEngine._locate_output(info, tmp_path, audio_only=False) == f.resolve()


def test_locate_output_fallback_newest(tmp_path: Path) -> None:
    old = tmp_path / "old.mp4"
    new = tmp_path / "new.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    info = {"requested_downloads": [{"filepath": str(tmp_path / "missing.mp4")}]}
    assert YtDlpEngine._locate_output(info, tmp_path, audio_only=False) == new.resolve()


def test_locate_output_raises_when_nothing(tmp_path: Path) -> None:
    info = {}
    try:
        YtDlpEngine._locate_output(info, tmp_path, audio_only=False)
    except DownloadError:
        return
    raise AssertionError("expected DownloadError")
