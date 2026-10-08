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

    def _opts(self, destination: Path, *, audio_only: bool, overwrite: bool) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "outtmpl": str(destination / "%(title).180B.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreconfig": True,
            "noprogress": True,  # progress is driven through hooks
            "progress_hooks": [self._hook],
            # Prefer H.264 so the result plays everywhere (Windows built-in
            # players do not decode AV1/HEVC without codec extensions).
            "format_sort": ["vcodec:h264"],
            "concurrent_fragment_downloads": 4,
            "ffmpeg_location": self.settings.resolve_ffmpeg(),
        }
        if self.settings.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.settings.cookies_from_browser,)
        elif self.settings.cookie_file:
            opts["cookiefile"] = str(self.settings.cookie_file.expanduser())
        if audio_only:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "nopostoverwrites": True,
                }
            ]
        else:
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mp4"
        if overwrite:
            opts["overwrites"] = True
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

    def _run(self, url: str, destination: Path, audio_only: bool, overwrite: bool) -> Path:
        import yt_dlp.utils  # noqa: PLC0415 - imported lazily to keep import cost out of CLI path
        from yt_dlp import YoutubeDL

        try:
            return self._run_once(url, destination, audio_only, overwrite, YoutubeDL)
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
                    return engine._run_once(url, destination, audio_only, overwrite, YoutubeDL)
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
    ) -> Path:
        import yt_dlp.utils  # noqa: PLC0415

        opts = self._opts(destination, audio_only=audio_only, overwrite=overwrite)
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
        # 2) safety net: newest matching file in the destination
        pattern = "*.m4a" if audio_only else "*.mp4"
        candidates = sorted(
            destination.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise DownloadError(f"yt-dlp finished but produced no {pattern} in {destination}")
        return candidates[0].resolve()

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        audio_only: bool = False,
        overwrite: bool = False,
    ) -> Path:
        destination = Path(destination).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        self._cancel.clear()
        # YoutubeDL is blocking; keep an async signature by running it in a thread.
        return await asyncio.to_thread(self._run, url, destination, audio_only, overwrite)
