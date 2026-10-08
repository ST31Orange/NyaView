from dataclasses import dataclass
from pathlib import Path

from nyavview.core.settings import Settings
from nyavview.utils import run_process
from nyavview.utils import safe_filename
from nyavview.utils.paths import ensure_directory
from nyavview.utils.paths import unique_path


@dataclass(frozen=True)
class M4sPair:
    video: Path
    audio: Path


class FfmpegTranscoder:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_file_args(
        self,
        source: Path,
        target: Path,
        *,
        overwrite: bool = False,
    ) -> list[str]:
        args = [
            str(self.settings.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y" if overwrite else "-n",
            "-i",
            str(source),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-b:a",
            self.settings.audio_bitrate,
            "-movflags",
            "+faststart",
        ]
        return args + [str(target)]

    async def transcode(
        self,
        source: Path,
        target_dir: Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        source = source.expanduser().resolve()
        target_dir = target_dir.expanduser().resolve()
        ensure_directory(target_dir)
        target = target_dir / f"{safe_filename(source.stem)}.mp4"
        if not overwrite:
            target = unique_path(target)
        result = await run_process(
            self.build_file_args(source, target, overwrite=overwrite)
        )
        if not result.ok:
            raise RuntimeError(result.stderr or "ffmpeg failed")
        return target

    async def transcode_bilibili_cache(
        self,
        source: Path,
        target_dir: Path,
        *,
        overwrite: bool = False,
    ) -> list[Path]:
        roots = self.cache_roots(source)
        outputs: list[Path] = []
        for root in roots:
            pair = self._find_m4s_pair(root)
            target = (
                target_dir / f"{safe_filename(self._title(root))}.mp4"
            )
            if not overwrite:
                target = unique_path(target)
            args = [
                str(self.settings.ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y" if overwrite else "-n",
                "-i",
                str(pair.video),
                "-i",
                str(pair.audio),
                "-map",
                "0:v?",
                "-map",
                "1:a?",
                "-c:v",
                "libx264",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
            ]
            args += [str(target)]
            result = await run_process(args)
            if not result.ok:
                raise RuntimeError(result.stderr or "ffmpeg failed")
            outputs.append(target)
        return outputs

    def cache_roots(self, source: Path) -> list[Path]:
        """Return Bilibili cache folders matching `entry.json` under source."""
        source = source.expanduser().resolve()
        if source.is_file():
            raise ValueError("cache conversion needs folders")
        if self._is_cache_root(source):
            return [source]
        roots = [
            child
            for child in sorted(source.iterdir())
            if child.is_dir() and self._is_cache_root(child)
        ]
        if not roots:
            raise ValueError(f"No Bilibili cache folder found in {source}")
        return roots

    @staticmethod
    def _is_cache_root(path: Path) -> bool:
        return any(path.rglob("entry.json"))

    @staticmethod
    def _find_m4s_pair(root: Path) -> M4sPair:
        videos = sorted(root.rglob("video.m4s"))
        if not videos:
            raise FileNotFoundError(f"No video.m4s found in {root}")
        video = videos[0]
        audio = video.parent / "audio.m4s"
        if not audio.exists():
            raise FileNotFoundError(f"No audio.m4s found in {video.parent}")
        return M4sPair(video=video, audio=audio)

    @staticmethod
    def _title(root: Path) -> str:
        entry_files = sorted(root.rglob("entry.json"))
        if not entry_files:
            return root.name
        import json
        entry = json.loads(entry_files[0].read_text(encoding="utf-8"))
        page = entry.get("page_data") or {}
        title = entry.get("title") or entry.get("videoTitle") or page.get("part")
        return title or root.name
