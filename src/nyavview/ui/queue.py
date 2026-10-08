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
from nyavview.core.errors import CanceledError, NyaViewError
from nyavview.core.events import ProgressEvent
from nyavview.core.settings import Settings
from nyavview.engines import FfmpegTranscoder, YtDlpEngine


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
        self._emit({"jobId": job.id, "type": "progress", **event.to_dict()})

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

    # ---------------------------------------------------------------- tasks

    def _job_settings(self, options: dict[str, Any]) -> Settings:
        """Clone settings with per-job option overrides applied."""
        overrides: dict[str, Any] = {}
        if options.get("preset"):
            overrides["video_preset"] = str(options["preset"])
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
        try:
            job.status = JobStatus.RUNNING
            self._emit({"jobId": job.id, "type": "status", "status": "running"})
            engine = YtDlpEngine(settings, on_progress=lambda e: self._progress(job, e))
            job.engine = engine
            job.output = await engine.download(
                url,
                destination,
                audio_only=bool(options.get("audioOnly")),
                overwrite=bool(options.get("overwrite")),
            )
            self._finish(job)
        except asyncio.CancelledError:
            # External task.cancel() (ffmpeg subprocess kill path). Mark the
            # job cancelled so the UI is not left with a stuck "running" card.
            self._finish(job, cancelled=True)
            raise
        except CanceledError:
            self._finish(job, cancelled=True)
        except NyaViewError as exc:
            self._finish(job, error=str(exc))
        except Exception as exc:  # defensive: never lose a job silently
            self._finish(job, error=f"{type(exc).__name__}: {exc}")

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

    def poll(self) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [job.to_dict() for job in self._jobs.values()]
        return sorted(jobs, key=lambda j: j["createdAt"], reverse=True)
