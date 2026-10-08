"""FFmpeg engine: local file transcode and Bilibili cache merge.

Improvements over the previous version:
- a single argument generator ``_build_args`` replaces the duplicated command
  construction (the local and Bilibili paths used to build nearly identical
  lists by hand);
- ``transcode_bilibili_cache`` now creates the target directory (it was
  missing before), runs conversions concurrently bounded by
  ``settings.max_concurrent_tasks`` (a setting that previously was dead code),
  and scans for ``entry.json`` with bounded depth instead of a full ``rglob``;
- progress is read live from ffmpeg's ``-progress pipe:1`` output.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional, Sequence

from nyavview.core.errors import TranscodeError
from nyavview.core.events import ProgressEvent
from nyavview.core.settings import Settings
from nyavview.utils import run_process, safe_filename
from nyavview.utils.paths import ensure_directory, unique_path

ProgressCallback = Callable[[ProgressEvent], None]

AudioMode = Literal["encode", "copy"]


@dataclass(frozen=True)
class M4sPair:
    video: Path
    audio: Path


class FfmpegTranscoder:
    def __init__(self, settings: Settings, *, on_progress: Optional[ProgressCallback] = None):
        self.settings = settings
        self._on_progress = on_progress

    # ------------------------------------------------------------------ emit

    def _emit(
        self,
        status: str,
        *,
        progress: Optional[float] = None,
        percent: Optional[float] = None,
        speed: Optional[str] = None,
        eta: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        if self._on_progress is None:
            return
        self._on_progress(
            ProgressEvent(
                kind="transcode",
                status=status,
                progress=progress,
                percent=percent,
                speed=speed,
                eta=eta,
                detail=detail,
            )
        )

    # ---------------------------------------------------------------- args

    def _ffmpeg(self) -> str:
        return self.settings.resolve_ffmpeg()

    def _build_args(
        self,
        inputs: Sequence[Path],
        target: Path,
        *,
        audio_mode: AudioMode,
        overwrite: bool,
    ) -> list[str]:
        """Build one ffmpeg command for any number of inputs.

        Mapping convention matches the previous behaviour:
        - single input: ``0:v?`` + ``0:a?`` (local file transcode);
        - two inputs: ``0:v?`` (video) + ``1:a?`` (audio) (Bilibili cache).
        """
        args: list[str] = [
            self._ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y" if overwrite else "-n",
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        for source in inputs:
            args += ["-i", str(source)]
        audio_map = "1:a?" if len(inputs) > 1 else "0:a?"
        args += ["-map", "0:v?", "-map", audio_map]
        args += ["-c:v", "libx264", "-preset", self.settings.video_preset]
        # Maximum-compatibility H.264: force 8-bit 4:2:0 (10-bit / 4:4:4
        # sources would otherwise be preserved and break old players such as
        # QQ影音), keep High profile, and use the known-good constant quality
        # (CRF 20 matches the manually merged Bilibili DASH file that plays in
        # every player).
        args += ["-pix_fmt", "yuv420p", "-profile:v", "high"]
        args += ["-crf", str(self.settings.video_crf)]
        args += ["-c:a", "aac" if audio_mode == "encode" else "copy"]
        if audio_mode == "encode":
            args += ["-b:a", self.settings.audio_bitrate]
        args += ["-movflags", "+faststart", str(target)]
        return args

    # ------------------------------------------------------------- progress

    async def _run_ffmpeg(self, args: list[str], *, duration: Optional[float], label: str) -> None:
        """Run ffmpeg, streaming ``-progress`` output into progress events."""
        state = {"at": 0.0}

        def on_line(stream: str, line: str) -> None:
            if stream != "out":
                return
            if line.startswith("out_time_us="):
                try:
                    secs = float(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    return
                now = time.monotonic()
                if now - state["at"] < 0.2:  # throttle to ~5 Hz
                    return
                state["at"] = now
                pct = (secs / duration * 100.0) if duration else None
                self._emit(
                    "running",
                    progress=(pct / 100.0 if pct is not None else None),
                    percent=pct,
                    detail=label,
                )

        result = await run_process(args, on_line=on_line)
        if not result.ok:
            raise TranscodeError(result.stderr.strip() or "ffmpeg failed")

    async def _probe_duration(self, path: Path) -> Optional[float]:
        """Quick header-only duration probe (no full decode)."""
        try:
            result = await run_process(
                [self._ffmpeg(), "-hide_banner", "-i", str(path)], timeout=30
            )
        except Exception:
            return None
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    # ------------------------------------------------------------ transcode

    async def _transcode_to(
        self,
        inputs: Sequence[Path],
        target: Path,
        *,
        audio_mode: AudioMode,
        overwrite: bool,
        label: str,
        probe_source: Optional[Path] = None,
    ) -> Path:
        """Encode to a temp file next to the target, then rename atomically.

        If ffmpeg is interrupted (cancel / app close), only the temp file is
        left behind — the final ``.mp4`` is never a truncated, unopenable file.
        """
        duration = await self._probe_duration(probe_source or inputs[0])
        tmp = target.with_name(f".{target.stem}.part-{os.getpid()}{target.suffix}")
        try:
            args = self._build_args(inputs, tmp, audio_mode=audio_mode, overwrite=True)
            self._emit("running", detail=label)
            await self._run_ffmpeg(args, duration=duration, label=label)
            if target.exists() and not overwrite:
                target = unique_path(target)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._emit("finished", progress=1.0, percent=100.0, detail=str(target))
        return target

    async def transcode(
        self,
        source: Path,
        target_dir: Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        source = Path(source).expanduser().resolve()
        target_dir = ensure_directory(Path(target_dir).expanduser().resolve())
        target = target_dir / f"{safe_filename(source.stem)}.mp4"
        if not overwrite and target.exists():
            target = unique_path(target)
        return await self._transcode_to(
            [source],
            target,
            audio_mode="encode",
            overwrite=overwrite,
            label=f"正在转换 {source.name}",
        )

    async def transcode_bilibili_cache(
        self,
        source: Path,
        target_dir: Path,
        *,
        overwrite: bool = False,
    ) -> list[Path]:
        source = Path(source).expanduser().resolve()
        target_dir = ensure_directory(Path(target_dir).expanduser().resolve())
        roots = self.cache_roots(source)

        sem = asyncio.Semaphore(self.settings.max_concurrent_tasks)

        async def _one(root: Path) -> Path:
            async with sem:
                pair = self._find_m4s_pair(root)
                target = target_dir / f"{safe_filename(self._title(root))}.mp4"
                if not overwrite and target.exists():
                    target = unique_path(target)
                return await self._transcode_to(
                    [pair.video, pair.audio],
                    target,
                    audio_mode="copy",
                    overwrite=overwrite,
                    label=f"正在转换 {root.name}",
                    probe_source=pair.video,
                )

        return list(await asyncio.gather(*(_one(root) for root in roots)))

    # --------------------------------------------------------------- lookup

    def cache_roots(self, source: Path, max_depth: int = 8) -> list[Path]:
        """Return every Bilibili cache root under ``source`` (recursive).

        A cache root is a folder that directly contains ``entry.json`` plus a
        ``video.m4s``/``audio.m4s`` pair somewhere in its subtree. The search
        recurses (bounded by ``max_depth``) so caches nested any number of
        folders deep are found — e.g. ``parent/outer/缓存/entry.json``. Every
        matching folder is returned (全部转换、不遗漏), including multi-P caches
        where a parent holds ``entry.json`` and its per-P subfolders each hold
        their own. Folders with ``entry.json`` but no media pair (metadata-only
        parents of a multi-P cache) are skipped, so their per-P subfolders are
        converted instead of producing a duplicate of the first P.
        """
        source = Path(source).expanduser().resolve()
        if source.is_file():
            raise TranscodeError("cache conversion needs a folder, not a file")
        roots: list[Path] = []

        def collect(directory: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                children = sorted(directory.iterdir())
            except OSError:
                return
            for child in children:
                if not child.is_dir():
                    continue
                if (child / "entry.json").is_file():
                    roots.append(child)
                collect(child, depth + 1)

        if (source / "entry.json").is_file():
            roots.append(source)
        collect(source, 1)

        candidates = sorted(set(roots))
        valid = [r for r in candidates if self._has_m4s_pair(r)]
        if not valid:
            raise TranscodeError(
                f"No Bilibili cache folder (entry.json + video.m4s) found in {source} "
                f"(searched up to {max_depth} levels)"
            )
        return valid

    @staticmethod
    def _has_m4s_pair(root: Path) -> bool:
        """True when ``root`` owns an m4s pair: a ``video.m4s`` with a sibling
        ``audio.m4s`` that is NOT inside a nested ``entry.json`` folder (media
        under another cache folder belongs to that cache root, e.g. the per-P
        subfolders of a multi-P cache)."""
        for video in root.rglob("video.m4s"):
            if (video.parent / "audio.m4s").is_file():
                ancestor = video.parent
                nested = False
                while ancestor != root:
                    if (ancestor / "entry.json").is_file():
                        nested = True
                        break
                    ancestor = ancestor.parent
                if not nested:
                    return True
        return False

    @staticmethod
    def _find_m4s_pair(root: Path) -> M4sPair:
        videos = sorted(root.rglob("video.m4s"))
        if not videos:
            raise TranscodeError(f"No video.m4s found in {root}")
        video = videos[0]
        audio = video.parent / "audio.m4s"
        if not audio.exists():
            raise TranscodeError(f"No audio.m4s found in {video.parent}")
        return M4sPair(video=video, audio=audio)

    @staticmethod
    def _title(root: Path) -> str:
        entry_files = sorted(root.rglob("entry.json"))
        if not entry_files:
            return root.name
        try:
            entry = json.loads(entry_files[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return root.name
        page = entry.get("page_data") or {}
        title = entry.get("title") or entry.get("videoTitle") or page.get("part")
        return title or root.name
