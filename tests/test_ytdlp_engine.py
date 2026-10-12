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
    # default (auto) keeps the source's native format: preferredcodec 'best'
    assert opts["postprocessors"][0]["preferredcodec"] == "best"
    assert "merge_output_format" not in opts


def test_opts_audio_only_explicit_format() -> None:
    opts = _engine()._opts(Path("d"), audio_only=True, overwrite=False, audio_format="mp3")
    assert opts["postprocessors"][0]["preferredcodec"] == "mp3"


def test_opts_video() -> None:
    opts = _engine()._opts(Path("d"), audio_only=False, overwrite=False)
    assert opts["format"] == "bestvideo+bestaudio/best"
    assert opts["merge_output_format"] == "mp4"


def test_opts_registers_progress_hook() -> None:
    """The progress hook must be wired into yt-dlp opts (regression: it was
    written but never registered, so the UI showed no download progress)."""
    engine = _engine()
    opts = engine._opts(Path("d"), audio_only=False, overwrite=False)
    hooks = opts.get("progress_hooks")
    assert hooks is not None and len(hooks) == 1
    assert hooks[0].__func__ is engine._hook.__func__


def test_opts_multi_part() -> None:
    """Multi-part (多P): playlist_items enables the playlist and restricts P."""
    engine = _engine()
    opts = engine._opts(
        Path("d"), audio_only=False, overwrite=False, playlist_items="1-3"
    )
    assert opts["noplaylist"] is False
    assert opts["playlist_items"] == "1-3"
    # single P keeps noplaylist
    single = engine._opts(Path("d"), audio_only=False, overwrite=False)
    assert single["noplaylist"] is True


def test_opts_prefer_h264_default_and_off() -> None:
    engine = _engine()
    default = engine._opts(Path("d"), audio_only=False, overwrite=False)
    assert default["format_sort"] == ["vcodec:h264"]
    # prefer_h264=False must OMIT format_sort entirely (never None) —
    # yt-dlp's FormatSorter does tuple(format_sort) and crashes on None.
    best = engine._opts(Path("d"), audio_only=False, overwrite=False, prefer_h264=False)
    assert "format_sort" not in best


def test_opts_format_id_has_fallback_chain() -> None:
    """A picked format must fall back gracefully on later P's (multi-P): the
    preview only shows P1's formats, so P2+ may lack the picked format id.
    yt-dlp format '/'-chains try picked+bestaudio -> picked -> best."""
    engine = _engine()
    opts = engine._opts(Path("d"), audio_only=False, overwrite=False, format_id="100026")
    assert opts["format"] == "100026+bestaudio/100026/bestvideo+bestaudio/best"


def test_hook_emits_progress_event() -> None:
    events = []
    engine = YtDlpEngine(Settings(), on_progress=events.append)
    engine._hook(
        {
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes": 100,
            "speed": 1024 * 1024,
            "eta": 5,
            "info_dict": {"title": "示例视频"},
        }
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "download"
    assert ev.status == "downloading"
    assert ev.percent == 50.0
    assert ev.progress == 0.5
    assert ev.detail == "示例视频"


def test_hook_emits_finished() -> None:
    events = []
    engine = YtDlpEngine(Settings(), on_progress=events.append)
    engine._hook({"status": "finished"})
    assert len(events) == 1
    assert events[0].status == "downloading"
    assert events[0].percent == 100.0


def test_is_cookie_error() -> None:
    engine = YtDlpEngine(Settings())
    assert engine._is_cookie_error("ERROR: Could not copy Chrome cookie database. See ...")
    assert engine._is_cookie_error("ERROR: Failed to copy Cookies database to memory. See ...")
    assert not engine._is_cookie_error("ERROR: HTTP Error 403: Forbidden")
    assert not engine._is_cookie_error("")


def test_run_falls_back_when_cookie_db_locked(monkeypatch) -> None:
    """A locked browser cookie DB must not fail the download: retry without
    cookies (the user's Edge-cookie config with Edge running reproduces this)."""
    import yt_dlp.utils

    from nyavview.engines.ytdlp import YtDlpEngine

    settings = Settings(cookies_from_browser="edge")
    engine = YtDlpEngine(settings, on_progress=lambda e: None)

    calls: list[str] = []

    def fake_run_once(self, url, destination, audio_only, overwrite, ydl_cls,
                      playlist_items=None, prefer_h264=True, write_subs=False,
                      outtmpl=None, format_id=None, audio_format=None, resilient=False):
        calls.append(str(self.settings.cookies_from_browser))
        if self.settings.cookies_from_browser:
            raise yt_dlp.utils.DownloadError(
                "ERROR: Could not copy Chrome cookie database. See issue 7271"
            )
        return Path("downloaded.mp4")

    monkeypatch.setattr(YtDlpEngine, "_run_once", fake_run_once)
    result = engine._run("https://x", Path("d"), False, False)
    assert result == Path("downloaded.mp4")
    # first call had cookies, the fallback retried without them
    assert calls == ["edge", "None"]


def test_opts_cookie_precedence(tmp_path: Path) -> None:
    """The cached cookie FILE wins over the browser once exported — downloads
    no longer depend on the browser's open/closed state."""
    engine = YtDlpEngine(Settings(cookies_from_browser="edge", cookie_file=tmp_path / "c.txt"))
    opts = engine._opts(Path("d"), audio_only=False, overwrite=False)
    assert opts["cookiefile"] == str((tmp_path / "c.txt").expanduser())
    assert "cookiesfrombrowser" not in opts


def test_opts_cookie_browser_fallback(tmp_path: Path) -> None:
    """Without a cookie file, the browser is used."""
    engine = YtDlpEngine(Settings(cookies_from_browser="edge"))
    opts = engine._opts(Path("d"), audio_only=False, overwrite=False)
    assert opts["cookiesfrombrowser"] == ("edge",)


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


# ------------------------------------------------ interrupted transfers

def test_incomplete_download_is_treated_as_transient() -> None:
    """A Spanish user hit:
       ERROR: [download] Got error: Downloaded 224660 bytes, expected 50910358 bytes
    (yt-dlp ContentTooShortError). It is a transient network failure, but it
    matched none of the old keywords, so NyaView gave up on the first attempt
    and never used its configured retries."""
    from nyavview.ui.queue import _is_transient_download_error

    assert _is_transient_download_error(
        "ERROR: [download] Got error: Downloaded 224660 bytes, expected 50910358 bytes"
    )
    assert _is_transient_download_error("content too short (expected 100 and served 20)")
    assert _is_transient_download_error("('Connection aborted.', ConnectionResetError(10054))")
    assert _is_transient_download_error("HTTP Error 503: Service Unavailable")
    assert _is_transient_download_error("The read operation timed out")
    # permanent failures must still be permanent
    assert not _is_transient_download_error("Unsupported URL: https://example.com/x")
    assert not _is_transient_download_error("This video is private")
    assert not _is_transient_download_error("Sign in to confirm you're not a bot")


def test_incomplete_download_error_carries_an_actionable_hint() -> None:
    from nyavview.ui.queue import _describe_download_error

    message = "ERROR: [download] Got error: Downloaded 224660 bytes, expected 50910358 bytes"
    described = _describe_download_error(message)
    assert described.startswith(message)
    assert len(described) > len(message)
    assert "网络" in described
    # unrelated errors are left untouched
    assert _describe_download_error("This video is private") == "This video is private"


def test_opts_resilient_only_applies_from_the_second_attempt() -> None:
    """The first attempt keeps yt-dlp's defaults; a retry switches to chunked,
    resumable downloading with a longer socket timeout."""
    engine = YtDlpEngine(Settings())
    destination = Path("downloads")

    plain = engine._opts(destination, audio_only=False, overwrite=False)
    for key in ("http_chunk_size", "socket_timeout", "retry_sleep_functions"):
        assert key not in plain, key

    retry = engine._opts(destination, audio_only=False, overwrite=False, resilient=True)
    assert retry["http_chunk_size"] == 10 * 1024 * 1024
    assert retry["socket_timeout"] >= 45
    assert retry["continuedl"] is True
    assert retry["nopart"] is False
    assert retry["retries"] >= 10
    # a struggling CDN is retried with a single connection
    assert retry["concurrent_fragment_downloads"] == 1
    assert callable(retry["retry_sleep_functions"]["http"])
    # backoff must grow but stay bounded
    sleep = retry["retry_sleep_functions"]["http"]
    assert sleep(1) <= sleep(3) <= 30


def test_download_forwards_resilient_to_the_engine(monkeypatch) -> None:
    """queue.py must pass resilient=True on retries; check the plumbing."""
    import asyncio

    engine = YtDlpEngine(Settings())
    seen: dict = {}

    def fake_run(url, destination, audio_only, overwrite, playlist_items=None,
                 prefer_h264=True, write_subs=False, outtmpl=None, format_id=None,
                 audio_format=None, resilient=False):
        seen["resilient"] = resilient
        return Path(destination) / "out.mp4"

    monkeypatch.setattr(engine, "_run", fake_run)
    result = asyncio.run(engine.download("https://example.com/v", Path("downloads"), resilient=True))
    assert seen["resilient"] is True
    assert result.name == "out.mp4"
