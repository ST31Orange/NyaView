"""yt-dlp engine built on the Python API instead of the CLI.

Why this change:
- the previous code shelled out to a hard-coded ``yt-dlp`` command and parsed
  the last JSON line of stdout to locate the output; that logic (a) referenced
  ``json`` without importing it (NameError at runtime), and (b) broke entirely
  when frozen into an EXE because ``yt-dlp.exe`` is not on PATH;
- the Python API provides ``progress_hooks`` natively, which the desktop UI
  consumes for live progress and cancellation.

The user-visible download behaviour (format selection, cookie handling, MP4
merging, M4A extraction, output template) is unchanged.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from nyavview.core.errors import CanceledError, DownloadError
from nyavview.core.events import ProgressEvent
from nyavview.core.settings import Settings

ProgressCallback = Callable[[ProgressEvent], None]


def _human_bytes(value: Optional[float]) -> Optional[str]:
    if not value:
        return None
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PiB"


_THUMBNAIL_MAX_BYTES = 3 * 1024 * 1024  # 3 MB cap on the preview thumbnail


def _thumbnail_data_uri(url: str, timeout: float = 12.0) -> str:
    """Download a thumbnail and return it as a base64 data URI.

    The WebView2 frontend fails to load remote (often http://) thumbnails, so
    the Python side fetches the image and inlines it. Falls back to the raw
    URL (or empty) on any failure so a broken thumbnail never blocks preview.
    """
    import base64

    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com/",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(_THUMBNAIL_MAX_BYTES + 1)
        if len(data) > _THUMBNAIL_MAX_BYTES:
            return url  # too big — let the browser try the raw URL
        content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0]
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{content_type};base64,{b64}"
    except Exception:
        return url  # fall back to the raw URL rather than failing preview


class YtDlpEngine:
    def __init__(self, settings: Settings, *, on_progress: Optional[ProgressCallback] = None):
        self.settings = settings
        self._on_progress = on_progress
        # Progress hooks run in the worker thread; use a thread-safe flag.
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _emit(
        self,
        status: str,
        *,
        progress: Optional[float] = None,
        percent: Optional[float] = None,
        speed: Optional[str] = None,
        eta: Optional[object] = None,
        detail: Optional[str] = None,
    ) -> None:
        if self._on_progress is None:
            return
        self._on_progress(
            ProgressEvent(
                kind="download",
                status=status,
                progress=progress,
                percent=percent,
                speed=speed,
                eta=str(eta) if eta is not None else None,
                detail=detail,
            )
        )

    def _opts(
        self,
        destination: Path,
        *,
        audio_only: bool,
        overwrite: bool,
        playlist_items: Optional[str] = None,
        prefer_h264: bool = True,
        write_subs: bool = False,
        outtmpl: Optional[str] = None,
        format_id: Optional[str] = None,
        audio_format: Optional[str] = None,
        resilient: bool = False,
    ) -> dict[str, Any]:
        opts: dict[str, Any] = {
            # Custom naming template (H) or default "<title>.180B.<ext>".
            "outtmpl": str(destination / (outtmpl or "%(title).180B.%(ext)s")),
            # Multi-part (Bilibili 多P): keep playlist semantics but restrict
            # to the requested range; when None, single video (first P only).
            "noplaylist": playlist_items is None,
            "quiet": True,
            "no_warnings": True,
            "ignoreconfig": True,
            "noprogress": True,  # progress is driven through hooks
            "progress_hooks": [self._hook],
            "concurrent_fragment_downloads": 4,
            "ffmpeg_location": self.settings.resolve_ffmpeg(),
        }
        # Default: prefer H.264 so the result plays everywhere (Windows
        # built-in players do not decode AV1/HEVC without codec extensions).
        # prefer_h264=False keeps yt-dlp's best-quality sort. NOTE: the key
        # must be OMITTED (not set to None) — yt-dlp's FormatSorter does
        # tuple(format_sort), which crashes with TypeError on None.
        if prefer_h264:
            opts["format_sort"] = ["vcodec:h264"]
        if playlist_items:
            opts["playlist_items"] = playlist_items
        if write_subs:
            # Subtitles (G): embed best subtitles; Bilibili CC + site subs.
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = ["zh-Hans", "zh-CN", "zh", "en"]
            opts["embedsubtitles"] = False
            opts["skip_removing_subtitles"] = False
        # Cookie precedence: a cached cookie FILE wins once it has been
        # exported (browser-agnostic, no lock issues), else read from the
        # browser (requires the browser to be closed).
        if self.settings.cookie_file:
            opts["cookiefile"] = str(self.settings.cookie_file.expanduser())
        elif self.settings.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.settings.cookies_from_browser,)
        if audio_only:
            opts["format"] = "bestaudio/best"
            # Audio export format (仅音频). 'auto' keeps the source's native
            # audio format without re-encoding (fastest, lossless); anything
            # else converts via yt-dlp's FFmpegExtractAudio.
            codec = (audio_format or "auto").lower()
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "best" if codec == "auto" else codec,
                    "nopostoverwrites": True,
                }
            ]
        else:
            if format_id:
                # User picked a specific format from the preview (C). The
                # preview only shows the FIRST P's formats, so a multi-P
                # download may not have this format on later P's — use a
                # yt-dlp fallback chain: picked+bestaudio → picked alone →
                # best, so each P still downloads with the best available.
                opts["format"] = f"{format_id}+bestaudio/{format_id}/bestvideo+bestaudio/best"
            else:
                opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mp4"
        if overwrite:
            opts["overwrites"] = True
        if resilient:
            # Used from the second attempt on: the first attempt failed with an
            # interrupted transfer ("Downloaded N bytes, expected M bytes"), so
            # make the retry much harder to break.
            #
            # * http_chunk_size makes yt-dlp pull the file in chunks and resume
            #   at the chunk boundary. Each completed chunk resets the retry
            #   counter (http.py: "except NextFragment: retry.attempt -= 1"), so
            #   a long download survives repeated interruptions instead of
            #   burning through the retry budget.
            # * A longer socket timeout tolerates a slow/stalling CDN, which is
            #   the usual reason an overseas connection to Bilibili gets cut.
            # * resumedl keeps whatever was already fetched.
            opts.setdefault("retries", 10)
            opts.setdefault("fragment_retries", 10)
            opts["socket_timeout"] = max(int(opts.get("socket_timeout") or 0), 45)
            opts["http_chunk_size"] = 10 * 1024 * 1024  # 10 MiB chunks
            opts["continuedl"] = True
            opts["nopart"] = False
            # Some CDNs throttle or drop clients that open several connections at
            # once, so retry with a single stream.
            opts["concurrent_fragment_downloads"] = 1
            # Back off gradually instead of hammering a struggling CDN.
            opts["retry_sleep_functions"] = {"http": lambda n: min(2 ** n, 30)}
        return opts

    def _hook(self, data: dict[str, Any]) -> None:
        if self._cancel.is_set():
            import yt_dlp.utils

            raise yt_dlp.utils.DownloadCancelled()
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100.0) if total else None
            self._emit(
                "downloading",
                progress=(percent / 100.0 if percent is not None else None),
                percent=percent,
                speed=_human_bytes(data.get("speed")),
                eta=data.get("eta"),
                detail=self._filename_of(data),
            )
        elif status == "finished":
            self._emit("downloading", progress=1.0, percent=100.0, detail="下载完成，正在合并 / 后处理…")

    @staticmethod
    def _filename_of(data: dict[str, Any]) -> Optional[str]:
        info = data.get("info_dict") or {}
        return info.get("title") or Path(data.get("filename") or "").name or None

    @staticmethod
    def _is_cookie_error(message: str) -> bool:
        """Detect yt-dlp errors caused by unreadable browser cookie DBs."""
        lowered = (message or "").lower()
        return "cookie" in lowered and ("could not copy" in lowered or "database" in lowered)

    def _run(
        self,
        url: str,
        destination: Path,
        audio_only: bool,
        overwrite: bool,
        playlist_items: Optional[str] = None,
        prefer_h264: bool = True,
        write_subs: bool = False,
        outtmpl: Optional[str] = None,
        format_id: Optional[str] = None,
        audio_format: Optional[str] = None,
        resilient: bool = False,
    ) -> Path:
        import yt_dlp.utils  # noqa: PLC0415 - imported lazily to keep import cost out of CLI path
        from yt_dlp import YoutubeDL

        try:
            return self._run_once(
                url, destination, audio_only, overwrite, YoutubeDL,
                playlist_items, prefer_h264, write_subs, outtmpl, format_id, audio_format,
                resilient,
            )
        except yt_dlp.utils.DownloadError as exc:
            message = str(exc)
            # Browser cookie DBs are locked while the browser runs. Retry
            # without cookies so the download still succeeds (possibly lower
            # quality) instead of failing outright.
            if self._is_cookie_error(message) and (self.settings.cookies_from_browser or self.settings.cookie_file):
                self._emit(
                    "downloading",
                    detail="浏览器 Cookie 读取失败（浏览器可能在运行），已自动切换为无 Cookie 下载…",
                )
                original = self.settings.model_copy(
                    update={"cookies_from_browser": None, "cookie_file": None}
                )
                # Reuse the same cancel event so a user cancel during the
                # fallback still aborts this retry.
                engine = YtDlpEngine(original, on_progress=self._on_progress)
                engine._cancel = self._cancel
                try:
                    return engine._run_once(
                        url, destination, audio_only, overwrite, YoutubeDL,
                        playlist_items, prefer_h264, write_subs, outtmpl, format_id, audio_format,
                        resilient,
                    )
                except yt_dlp.utils.DownloadError as retry_exc:
                    raise DownloadError(str(retry_exc)) from retry_exc
            raise DownloadError(message) from exc

    def _run_once(
        self,
        url: str,
        destination: Path,
        audio_only: bool,
        overwrite: bool,
        ydl_cls,
        playlist_items: Optional[str] = None,
        prefer_h264: bool = True,
        write_subs: bool = False,
        outtmpl: Optional[str] = None,
        format_id: Optional[str] = None,
        audio_format: Optional[str] = None,
        resilient: bool = False,
    ) -> Path:
        import yt_dlp.utils  # noqa: PLC0415

        opts = self._opts(
            destination,
            audio_only=audio_only,
            overwrite=overwrite,
            playlist_items=playlist_items,
            prefer_h264=prefer_h264,
            write_subs=write_subs,
            outtmpl=outtmpl,
            format_id=format_id,
            audio_format=audio_format,
            resilient=resilient,
        )
        try:
            with ydl_cls(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if isinstance(info, list):
                    info = info[-1]
                return self._locate_output(info, destination, audio_only)
        except yt_dlp.utils.DownloadCancelled:
            raise CanceledError("download cancelled by user") from None
        except yt_dlp.utils.DownloadError as exc:
            raise

    @staticmethod
    def _locate_output(info: Any, destination: Path, audio_only: bool) -> Path:
        # 1) trust yt-dlp's own report of where the file landed
        try:
            path = info["requested_downloads"][0]["filepath"]
            candidate = Path(path)
            if candidate.exists():
                return candidate.resolve()
        except (KeyError, IndexError, TypeError):
            pass
        # 2) safety net: newest finished (non-temp) file in the destination.
        #    Audio-only output extension varies by chosen format (mp3/aac/m4a/
        #    flac/wav/opus/webm…), so match any non-partial file.
        skip = (".part", ".ytdl", ".tmp", ".temp")
        candidates = sorted(
            (
                p
                for p in destination.iterdir()
                if p.is_file()
                and not p.name.lower().endswith(skip)
                and (audio_only or p.suffix.lower() == ".mp4")
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise DownloadError(f"yt-dlp finished but produced no output in {destination}")
        return candidates[0].resolve()

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        audio_only: bool = False,
        overwrite: bool = False,
        playlist_items: Optional[str] = None,
        prefer_h264: bool = True,
        write_subs: bool = False,
        outtmpl: Optional[str] = None,
        format_id: Optional[str] = None,
        audio_format: Optional[str] = None,
        resilient: bool = False,
    ) -> Path:
        destination = Path(destination).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        self._cancel.clear()
        # YoutubeDL is blocking; keep an async signature by running it in a thread.
        return await asyncio.to_thread(
            self._run,
            url,
            destination,
            audio_only,
            overwrite,
            playlist_items,
            prefer_h264,
            write_subs,
            outtmpl,
            format_id,
            audio_format,
            resilient,
        )

    async def preview(self, url: str, prefer_h264: bool = True) -> dict[str, Any]:
        """Extract metadata without downloading (C/E: formats, title, thumb).

        When ``prefer_h264`` is set the format list is filtered to H.264
        (AVC) video formats only, so the UI picker matches the download's
        ``prefer_h264`` mode — picking from the list never yields an
        AV1/HEVC stream the user cannot play.
        """
        import asyncio as _aio
        import yt_dlp.utils

        def _extract(settings: Settings) -> dict[str, Any]:
            from yt_dlp import YoutubeDL

            engine = YtDlpEngine(settings)
            opts = engine._opts(
                Path("."),
                audio_only=False,
                overwrite=False,
                playlist_items=None,
                prefer_h264=prefer_h264,
            )
            opts["skip_download"] = True
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if isinstance(info, list):
                info = info[-1] if info else {}
            if info and info.get("entries") and not info.get("_type") == "playlist":
                info = info["entries"][0] if info["entries"] else info
            entries = info.get("entries") or []
            formats = []
            seen = set()
            for f in (info.get("formats") or []):
                if not f.get("format_id"):
                    continue
                key = (f.get("format_id"), f.get("height"), f.get("vcodec"), f.get("acodec"))
                if key in seen:
                    continue
                seen.add(key)
                vcodec = f.get("vcodec") or "audio"
                if vcodec == "none":
                    continue  # skip audio-only formats in the video list
                codec = (vcodec or "").split(".")[0].lower()
                if prefer_h264 and codec != "avc1":
                    continue  # H.264 mode: only list AVC video formats
                formats.append(
                    {
                        "id": f.get("format_id"),
                        "height": f.get("height"),
                        "fps": f.get("fps"),
                        "vcodec": codec,
                        "ext": f.get("ext"),
                        "filesize": f.get("filesize") or f.get("filesize_approx"),
                    }
                )
            formats.sort(key=lambda x: (x["height"] or 0, x["fps"] or 0), reverse=True)
            # dedupe by height keeping the first (best) entry
            by_height: dict[Any, Any] = {}
            for fmt in formats:
                by_height.setdefault(fmt["height"], fmt)
            return {
                "title": info.get("title"),
                "uploader": info.get("uploader") or info.get("channel"),
                "duration": info.get("duration"),
                "thumbnail": _thumbnail_data_uri(info.get("thumbnail") or ""),
                "playlistCount": len(entries),
                "formats": list(by_height.values()),
            }

        def _run() -> dict[str, Any]:
            try:
                return _extract(self.settings)
            except yt_dlp.utils.DownloadError as exc:
                # Browser cookie DB locked (issue #7271): preview only needs
                # public metadata, so fall back to a no-cookie extraction
                # instead of surfacing the lock error.
                if self._is_cookie_error(str(exc)) and (
                    self.settings.cookies_from_browser or self.settings.cookie_file
                ):
                    original = self.settings.model_copy(
                        update={"cookies_from_browser": None, "cookie_file": None}
                    )
                    return _extract(original)
                raise

        return await _aio.to_thread(_run)
