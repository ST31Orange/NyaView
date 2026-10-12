"""Background job manager for the desktop UI.

Runs an asyncio event loop in a dedicated thread so that the pywebview main
thread stays responsive. Engines emit :class:`ProgressEvent` instances which
are queued (thread-safe) and polled by the JS frontend via ``api.poll()``.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from nyavview.core import service
from nyavview.core.errors import CanceledError, NyaViewError, TranscodeError
from nyavview.core.events import ProgressEvent
from nyavview.core.settings import Settings
from nyavview.engines import EbookBuilder, FfmpegTranscoder, YtDlpEngine


# yt-dlp reports a transfer that was cut short as
#   "ERROR: [download] Got error: Downloaded 224660 bytes, expected 50910358 bytes"
# (ContentTooShortError). That is a transient network failure — yt-dlp retries
# it and resumes from the partial file — but the message matches none of the
# obvious keywords, so NyaView used to classify it as permanent and never used
# its own retries at all.
_TRANSIENT_DOWNLOAD_MARKERS = (
    # an interrupted transfer
    "expected", "content too short", "incomplete", "unable to download",
    "connection reset", "connection aborted", "connection refused",
    "broken pipe", "eof occurred", "read timed out",
    # general network trouble
    "network", "timeout", "timed out", "temporar", "connection", "socket",
    "http error 5", "http error 429", "http error 403",
    "502", "503", "504", "429",
)

_INCOMPLETE_MARKERS = ("expected", "content too short", "incomplete")

_DOWNLOAD_HINT = (
    "\n下载连接被中途切断（服务器只传了部分数据就断开，不是文件本身的问题）。"
    "已自动重试并改用分块续传；若仍失败，通常是本机到该站点的网络链路不稳定：\n"
    "  · 在境外访问 B 站时尤其常见 → 可尝试代理 / 换网络 / 稍后再试\n"
    "  · 或先降低画质（如 720P）、或改用「仅音频」\n"
    "  · 若开了 VPN / 代理，试试关掉或换节点"
)


def _is_transient_download_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_DOWNLOAD_MARKERS)


def _describe_download_error(message: str) -> str:
    """Add an actionable hint for interrupted-download failures."""
    lowered = (message or "").lower()
    if any(marker in lowered for marker in _INCOMPLETE_MARKERS):
        return message + _DOWNLOAD_HINT
    return message


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class Job:
    def __init__(self, job_id: str, kind: str, label: str) -> None:
        self.id = job_id
        self.kind = kind  # "download" | "convert" | "bili"
        self.label = label
        self.status = JobStatus.PENDING
        self.progress: Optional[float] = None
        self.percent: Optional[float] = None
        self.speed: Optional[str] = None
        self.eta: Optional[str] = None
        self.detail: Optional[str] = None
        self.error: Optional[str] = None
        self.output: Optional[Path] = None
        self.created_at = time.time()
        # runtime handles
        self.task: Optional[asyncio.Future] = None
        self.engine: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status.value,
            "progress": self.progress,
            "percent": self.percent,
            "speed": self.speed,
            "eta": self.eta,
            "detail": self.detail,
            "error": self.error,
            "output": str(self.output) if self.output else None,
            "createdAt": self.created_at,
        }


class JobManager:
    """Owns the background loop, the job registry, and the event queue."""

    # pywebview's get_functions() recursively walks API-object attributes; mark
    # this object as non-serializable so the bridge only exposes real methods.
    _serializable = False

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings.discover()
        self._jobs: dict[str, Job] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=1000)
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ids = itertools.count(1)
        # One-shot "a file/folder was dropped" slot, filled by the pywebview
        # DOM drop handler (which receives absolute paths) and drained by the
        # JS frontend via api.poll() — Python→JS evaluate_js would deadlock
        # while the page polls, so drops travel back through the poll channel.
        self._pending_drop: Optional[tuple[str, str]] = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="nyaview-jobs", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None:
            pending = [
                job.task
                for job in self._jobs.values()
                if job.task is not None and not job.task.done()
            ]
            for future in pending:
                future.cancel()
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._loop = None
        self._thread = None

    # ------------------------------------------------------------ internals

    def _next_id(self) -> str:
        return str(next(self._ids))

    def _emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(event)

    def emit_log(self, level: str, message: str) -> None:
        """Push a plain log line to the UI's log panel (no job involved)."""
        self._emit({"type": "log", "level": level, "message": message})

    def _register(self, kind: str, label: str) -> Job:
        job = Job(self._next_id(), kind, label)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def _progress(self, job: Job, event: ProgressEvent) -> None:
        job.progress = event.progress
        job.percent = event.percent
        job.speed = event.speed
        job.eta = event.eta
        if event.detail:
            job.detail = event.detail
        payload = event.to_dict()
        # The engine reports its own flavour of progress (both the ebook and the
        # merge jobs run on EbookBuilder, which always says "ebook"). The badge
        # must show the JOB's kind, so the event carries that instead.
        payload["kind"] = job.kind
        self._emit({"jobId": job.id, "type": "progress", **payload})

    def _finish(self, job: Job, *, error: Optional[str] = None, cancelled: bool = False) -> None:
        if cancelled:
            job.status = JobStatus.CANCELLED
        elif error:
            job.status = JobStatus.ERROR
            job.error = error
        else:
            job.status = JobStatus.DONE
        self._emit(
            {
                "jobId": job.id,
                "type": "status",
                "status": job.status.value,
                "error": job.error,
                "output": str(job.output) if job.output else None,
            }
        )

    # --------------------------------------------------------------- submit

    def submit_download(self, url: str, options: dict[str, Any]) -> str:
        job = self._register("download", url)
        assert self._loop is not None
        job.task = asyncio.run_coroutine_threadsafe(self._run_download(job, url, options), self._loop)
        return job.id

    def submit_convert(self, source: str, options: dict[str, Any]) -> str:
        job = self._register("convert", Path(source).name)
        assert self._loop is not None
        job.task = asyncio.run_coroutine_threadsafe(self._run_convert(job, source, options), self._loop)
        return job.id

    def submit_bili(self, source: str, options: dict[str, Any]) -> str:
        job = self._register("bili", Path(source).name)
        assert self._loop is not None
        job.task = asyncio.run_coroutine_threadsafe(self._run_bili(job, source, options), self._loop)
        return job.id

    def submit_ebook(self, source: str, options: dict[str, Any]) -> str:
        label = options.get("title") or Path(source).name
        job = self._register("ebook", str(label))
        assert self._loop is not None
        job.task = asyncio.run_coroutine_threadsafe(self._run_ebook(job, source, options), self._loop)
        return job.id

    def submit_merge(self, files: list[str], options: dict[str, Any]) -> str:
        label = options.get("title") or f"{len(files)} 本合集"
        job = self._register("merge", str(label))
        assert self._loop is not None
        job.task = asyncio.run_coroutine_threadsafe(self._run_merge(job, files, options), self._loop)
        return job.id

    # ---------------------------------------------------------------- tasks

    def _job_settings(self, options: dict[str, Any]) -> Settings:
        """Clone settings with per-job option overrides applied."""
        overrides: dict[str, Any] = {}
        if options.get("preset"):
            overrides["video_preset"] = str(options["preset"])
        if options.get("videoCrf"):
            overrides["video_crf"] = int(options["videoCrf"])
        if options.get("audioBitrate"):
            overrides["audio_bitrate"] = str(options["audioBitrate"])
        if options.get("maxConcurrentTasks"):
            overrides["max_concurrent_tasks"] = int(options["maxConcurrentTasks"])
        if options.get("cookieFile"):
            overrides["cookie_file"] = Path(str(options["cookieFile"]))
            overrides["cookies_from_browser"] = None
        elif options.get("cookiesFromBrowser"):
            overrides["cookies_from_browser"] = str(options["cookiesFromBrowser"])
            overrides["cookie_file"] = None
        return self.settings.model_copy(update=overrides)

    async def _run_download(self, job: Job, url: str, options: dict[str, Any]) -> None:
        settings = self._job_settings(options)
        destination = Path(options.get("destination") or settings.download_dir)
        max_attempts = int(options.get("maxRetries", 3))
        try:
            job.status = JobStatus.RUNNING
            self._emit({"jobId": job.id, "type": "status", "status": "running"})
            engine = YtDlpEngine(settings, on_progress=lambda e: self._progress(job, e))
            job.engine = engine
            attempt = 0
            last_error: Optional[str] = None
            while True:
                attempt += 1
                if attempt > 1:
                    self._emit(
                        {"jobId": job.id, "type": "progress", "kind": "download",
                         "status": "downloading", "progress": None, "percent": None,
                         "speed": None, "eta": None,
                         "detail": f"第 {attempt - 1} 次失败，正在重试（{max_attempts - attempt + 1} 次剩余）…"}
                    )
                try:
                    job.output = await engine.download(
                        url,
                        destination,
                        audio_only=bool(options.get("audioOnly")),
                        overwrite=bool(options.get("overwrite")),
                        playlist_items=options.get("playlistItems") or None,
                        prefer_h264=bool(options.get("preferH264", True)),
                        write_subs=bool(options.get("writeSubs")),
                        outtmpl=options.get("outtmpl") or settings.naming_template,
                        format_id=options.get("format") or None,
                        audio_format=options.get("audioFormat") or None,
                        # From the 2nd attempt on: download in chunks with a
                        # longer socket timeout, so an interrupted transfer can
                        # resume instead of being cut at the same place again.
                        resilient=attempt > 1,
                        # Bilibili returns backup CDN hosts the web player falls
                        # back to but yt-dlp ignores; try them on retries
                        # (attempt 2 -> backup #1, attempt 3 -> backup #2, ...).
                        cdn_backup_index=attempt - 1,
                    )
                    break
                except (CanceledError, asyncio.CancelledError):
                    raise
                except NyaViewError as exc:
                    last_error = str(exc)
                    if attempt >= max_attempts:
                        raise
                    # transient errors only: retry on network-ish failures
                    if not _is_transient_download_error(last_error):
                        raise
                    await asyncio.sleep(2)
            self._finish(job)
        except asyncio.CancelledError:
            # External task.cancel() (ffmpeg subprocess kill path). Mark the
            # job cancelled so the UI is not left with a stuck "running" card.
            self._finish(job, cancelled=True)
            raise
        except CanceledError:
            self._finish(job, cancelled=True)
        except NyaViewError as exc:
            self._finish(job, error=_describe_download_error(str(exc)))
        except Exception as exc:  # defensive: never lose a job silently
            self._finish(job, error=_describe_download_error(f"{type(exc).__name__}: {exc}"))

    async def _run_convert(self, job: Job, source: str, options: dict[str, Any]) -> None:
        settings = self._job_settings(options)
        target_dir = Path(options.get("destination") or settings.converted_dir)
        try:
            job.status = JobStatus.RUNNING
            self._emit({"jobId": job.id, "type": "status", "status": "running"})
            engine = FfmpegTranscoder(settings, on_progress=lambda e: self._progress(job, e))
            job.engine = engine
            job.output = await engine.transcode(
                Path(source),
                target_dir,
                overwrite=bool(options.get("overwrite")),
            )
            self._finish(job)
        except asyncio.CancelledError:
            self._finish(job, cancelled=True)
            raise
        except CanceledError:
            self._finish(job, cancelled=True)
        except NyaViewError as exc:
            self._finish(job, error=str(exc))
        except Exception as exc:
            self._finish(job, error=f"{type(exc).__name__}: {exc}")

    async def _run_bili(self, job: Job, source: str, options: dict[str, Any]) -> None:
        settings = self._job_settings(options)
        target_dir = Path(options.get("destination") or settings.converted_dir)
        try:
            job.status = JobStatus.RUNNING
            self._emit({"jobId": job.id, "type": "status", "status": "running"})
            engine = FfmpegTranscoder(settings, on_progress=lambda e: self._progress(job, e))
            job.engine = engine
            outputs = await engine.transcode_bilibili_cache(
                Path(source),
                target_dir,
                overwrite=bool(options.get("overwrite")),
            )
            job.output = outputs[0] if outputs else None
            self._finish(job)
        except asyncio.CancelledError:
            self._finish(job, cancelled=True)
            raise
        except CanceledError:
            self._finish(job, cancelled=True)
        except NyaViewError as exc:
            self._finish(job, error=str(exc))
        except Exception as exc:
            self._finish(job, error=f"{type(exc).__name__}: {exc}")

    async def _run_ebook(self, job: Job, source: str, options: dict[str, Any]) -> None:
        settings = self._job_settings(options)
        target_dir = Path(options.get("destination") or settings.converted_dir)
        try:
            job.status = JobStatus.RUNNING
            self._emit({"jobId": job.id, "type": "status", "status": "running"})
            engine = EbookBuilder(settings, on_progress=lambda e: self._progress(job, e))
            job.engine = engine
            # The frontend sends the final page order (so manual reordering
            # wins); fall back to a fresh natural-sorted scan when absent.
            orders = options.get("orders")
            if not orders:
                info = engine.scan(Path(source), mode=options.get("mode") or "merge")
                groups = info["groups"]
            else:
                groups = [
                    {"name": g.get("name"), "images": list(g.get("images") or [])}
                    for g in orders
                    if g.get("images")
                ]
            if not groups:
                raise TranscodeError("没有可转换的图片")
            outputs = await engine.build(
                groups,
                target_dir,
                fmt=options.get("format") or "cbz",
                title=options.get("title") or "",
                author=options.get("author") or "",
                overwrite=bool(options.get("overwrite")),
                comic_info=bool(options.get("comicInfo", True)),
                name_template=options.get("nameTemplate") or "",
            )
            job.output = outputs[0] if outputs else None
            if len(outputs) > 1:
                self._emit(
                    {
                        "jobId": job.id,
                        "type": "progress",
                        "kind": job.kind,
                        "status": "running",
                        "progress": 1.0,
                        "percent": 100.0,
                        "speed": None,
                        "eta": None,
                        "detail": "共生成 " + str(len(outputs)) + " 个文件：" + "、".join(p.name for p in outputs),
                    }
                )
            self._finish(job)
        except asyncio.CancelledError:
            self._finish(job, cancelled=True)
            raise
        except CanceledError:
            self._finish(job, cancelled=True)
        except NyaViewError as exc:
            self._finish(job, error=str(exc))
        except Exception as exc:
            self._finish(job, error=f"{type(exc).__name__}: {exc}")

    async def _run_merge(self, job: Job, files: list[str], options: dict[str, Any]) -> None:
        settings = self._job_settings(options)
        target_dir = Path(options.get("destination") or settings.converted_dir)
        try:
            job.status = JobStatus.RUNNING
            self._emit({"jobId": job.id, "type": "status", "status": "running"})
            engine = EbookBuilder(settings, on_progress=lambda e: self._progress(job, e))
            job.engine = engine
            outputs = await engine.merge(
                [Path(f) for f in (files or [])],
                target_dir,
                fmt=options.get("format") or "epub",
                title=options.get("title") or "",
                author=options.get("author") or "",
                overwrite=bool(options.get("overwrite")),
                keep_source_toc=bool(options.get("keepSourceToc", True)),
            )
            job.output = outputs[0] if outputs else None
            self._finish(job)
        except asyncio.CancelledError:
            self._finish(job, cancelled=True)
            raise
        except CanceledError:
            self._finish(job, cancelled=True)
        except NyaViewError as exc:
            self._finish(job, error=str(exc))
        except Exception as exc:
            self._finish(job, error=f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- control
    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            return False
        if isinstance(job.engine, YtDlpEngine):
            # Graceful cancel: the progress hook raises DownloadCancelled
            # -> CanceledError, which the task handler marks as cancelled.
            # Do NOT task.cancel() here — that raises asyncio.CancelledError
            # inside the coroutine, bypassing the CanceledError handler and
            # leaving the job stuck in "running" with no status event.
            job.engine.cancel()
        elif isinstance(job.engine, EbookBuilder):
            # Cooperative flag: checked between books, so the file currently
            # being written finishes and is renamed atomically.
            job.engine.cancel()
        elif job.task is not None:
            # ffmpeg: cancelling the asyncio task kills the subprocess
            # (run_process handles CancelledError by killing the child).
            job.task.cancel()
        return True

    def clear_finished(self) -> int:
        with self._lock:
            finished = [
                jid
                for jid, job in self._jobs.items()
                if job.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)
            ]
            for jid in finished:
                del self._jobs[jid]
        return len(finished)

    # -------------------------------------------------------------- snapshot

    def set_pending_drop(self, target: str, path: str) -> None:
        """Record an absolute dropped path (from the pywebview DOM drop
        handler) for the JS frontend to pick up on its next poll."""
        with self._lock:
            self._pending_drop = (target, path)

    def poll(self) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
            if self._pending_drop is not None:
                target, path = self._pending_drop
                self._pending_drop = None
                events.insert(0, {"type": "drop", "target": target, "path": path})
        return events

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [job.to_dict() for job in self._jobs.values()]
        return sorted(jobs, key=lambda j: j["createdAt"], reverse=True)
