"""Queue-level tests: retry policy for interrupted downloads.

A Spanish user reported (translated):
    ERROR: [download] Got error: Downloaded 224660 bytes, expected 50910358 bytes

That is yt-dlp's ContentTooShortError — a transient network failure. It used to
match none of the retry keywords, so NyaView gave up on the first attempt and
never used its configured retries. These tests drive the whole JobManager path.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nyavview.core.errors import DownloadError
from nyavview.core.settings import Settings
from nyavview.engines.ytdlp import YtDlpEngine
from nyavview.ui.queue import JobManager

INTERRUPTED = (
    "ERROR: [download] Got error: Downloaded 224660 bytes, expected 50910358 bytes"
)


def _run_job(manager: JobManager, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {j["id"]: j for j in manager.list_jobs()}
        job = jobs.get(job_id)
        if job and job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


@pytest.fixture()
def manager(tmp_path: Path):
    mgr = JobManager()
    mgr.start()
    yield mgr
    mgr.stop()


def test_interrupted_download_is_retried_with_resilient_mode(manager, monkeypatch, tmp_path) -> None:
    """3 attempts, and from the 2nd one the engine switches to the resilient
    (chunked, resumable) download mode."""
    attempts: list[bool] = []

    async def fake_download(self, url, destination, **kwargs):
        attempts.append(bool(kwargs.get("resilient")))
        raise DownloadError(INTERRUPTED)

    monkeypatch.setattr(YtDlpEngine, "download", fake_download)

    job_id = manager.submit_download(
        "https://example.com/video",
        {"destination": str(tmp_path / "dl"), "maxRetries": 3},
    )
    job = _run_job(manager, job_id)

    assert attempts == [False, True, True], attempts
    assert job["status"] == "error"
    # the raw yt-dlp message is preserved, plus an actionable hint
    assert "Downloaded 224660 bytes, expected 50910358 bytes" in job["error"]
    assert "网络" in job["error"]


def test_permanent_errors_are_not_retried(manager, monkeypatch, tmp_path) -> None:
    attempts: list[bool] = []

    async def fake_download(self, url, destination, **kwargs):
        attempts.append(bool(kwargs.get("resilient")))
        raise DownloadError("ERROR: Unsupported URL: https://example.com/x")

    monkeypatch.setattr(YtDlpEngine, "download", fake_download)

    job_id = manager.submit_download(
        "https://example.com/x",
        {"destination": str(tmp_path / "dl"), "maxRetries": 3},
    )
    job = _run_job(manager, job_id)

    assert attempts == [False], "a permanent error must not be retried"
    assert job["status"] == "error"
    assert "网络" not in job["error"], "no network hint for a permanent error"


def test_recovering_transfer_succeeds_on_retry(manager, monkeypatch, tmp_path) -> None:
    """A connection that recovers: the first attempt fails, the retry succeeds."""
    attempts: list[bool] = []
    output = tmp_path / "dl" / "video.mp4"

    async def fake_download(self, url, destination, **kwargs):
        attempts.append(bool(kwargs.get("resilient")))
        if len(attempts) == 1:
            raise DownloadError(INTERRUPTED)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ok")
        return output

    monkeypatch.setattr(YtDlpEngine, "download", fake_download)

    job_id = manager.submit_download(
        "https://example.com/video",
        {"destination": str(tmp_path / "dl"), "maxRetries": 3},
    )
    job = _run_job(manager, job_id)

    assert attempts == [False, True]
    assert job["status"] == "done"
    assert job["output"] == str(output)


def test_retry_respects_the_configured_attempt_count(manager, monkeypatch, tmp_path) -> None:
    attempts: list[bool] = []

    async def fake_download(self, url, destination, **kwargs):
        attempts.append(bool(kwargs.get("resilient")))
        raise DownloadError(INTERRUPTED)

    monkeypatch.setattr(YtDlpEngine, "download", fake_download)

    job_id = manager.submit_download(
        "https://example.com/video",
        {"destination": str(tmp_path / "dl"), "maxRetries": 2},
    )
    _run_job(manager, job_id)
    assert attempts == [False, True]
