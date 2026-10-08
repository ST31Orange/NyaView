"""Structured exception hierarchy for NyaView.

Consumers (CLI, desktop UI, future REST) can catch a specific error instead
of pattern-matching on ``RuntimeError`` strings.
"""


class NyaViewError(Exception):
    """Base class for all NyaView errors."""


class ConfigurationError(NyaViewError):
    """Invalid or unusable configuration (missing file, bad path...)."""


class EngineError(NyaViewError):
    """An underlying engine (yt-dlp / ffmpeg) failed."""


class DownloadError(EngineError):
    """A download operation failed."""


class TranscodeError(EngineError):
    """A transcode operation failed."""


class CanceledError(EngineError):
    """An operation was cancelled by the user."""


class ProcessTimeoutError(EngineError):
    """A child process did not finish within the configured timeout."""

    def __init__(self, command: list[str], timeout: float | None) -> None:
        self.command = command
        self.timeout = timeout
        super().__init__(f"process timed out after {timeout!s}s: {command}")
