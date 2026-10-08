import json
from pathlib import Path

import pytest

from nyavview.core.errors import TranscodeError
from nyavview.core.settings import Settings
from nyavview.engines.ffmpeg import FfmpegTranscoder, M4sPair


def _engine(ffmpeg: str = "ffmpeg") -> FfmpegTranscoder:
    return FfmpegTranscoder(Settings(ffmpeg_path=Path(ffmpeg)))


def test_build_args_single_input_encode() -> None:
    args = _engine()._build_args(
        [Path("a.mkv")], Path("out/a.mp4"), audio_mode="encode", overwrite=False
    )
    assert args[0].endswith("ffmpeg")
    assert "-n" in args  # no overwrite
    assert "-y" not in args
    assert "-i" in args and args[args.index("-i") + 1] == "a.mkv"
    assert args[args.index("-map") + 1] == "0:v?"
    assert "1:a?" not in args
    assert "aac" in args
    assert "192k" in args  # default bitrate
    assert args[-1] == str(Path("out/a.mp4"))


def test_build_args_two_inputs_copy_audio() -> None:
    args = _engine()._build_args(
        [Path("v.m4s"), Path("a.m4s")], Path("out/v.mp4"), audio_mode="copy", overwrite=True
    )
    assert "-y" in args
    assert "-i" in args
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert "0:v?" in maps and "1:a?" in maps
    assert "copy" in args
    assert "192k" not in args  # bitrate only for encode mode


def test_build_args_uses_preset_and_bitrate() -> None:
    engine = FfmpegTranscoder(Settings(ffmpeg_path=Path("ffmpeg"), video_preset="fast", audio_bitrate="128k"))
    args = engine._build_args([Path("a.mkv")], Path("o.mp4"), audio_mode="encode", overwrite=False)
    assert args[args.index("-preset") + 1] == "fast"
    assert args[args.index("-b:a") + 1] == "128k"


def test_cache_roots_detects_direct_root(tmp_path: Path) -> None:
    (tmp_path / "entry.json").write_text("{}", encoding="utf-8")
    engine = _engine()
    assert engine.cache_roots(tmp_path) == [tmp_path]


def test_cache_roots_treats_parent_as_root_when_nested(tmp_path: Path) -> None:
    """A parent containing cache folders counts as a root itself (matches
    the original rglob-based semantics)."""
    one = tmp_path / "one"
    one.mkdir()
    (one / "entry.json").write_text("{}", encoding="utf-8")
    engine = _engine()
    assert engine.cache_roots(tmp_path) == [tmp_path]


def test_cache_roots_raises_when_none(tmp_path: Path) -> None:
    with pytest.raises(TranscodeError):
        _engine().cache_roots(tmp_path)


def test_cache_roots_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    with pytest.raises(TranscodeError):
        _engine().cache_roots(f)


def test_find_m4s_pair(tmp_path: Path) -> None:
    (tmp_path / "64").mkdir()
    (tmp_path / "64" / "video.m4s").write_bytes(b"v")
    (tmp_path / "64" / "audio.m4s").write_bytes(b"a")
    pair = FfmpegTranscoder._find_m4s_pair(tmp_path)
    assert pair == M4sPair(video=tmp_path / "64" / "video.m4s", audio=tmp_path / "64" / "audio.m4s")


def test_find_m4s_pair_missing_audio(tmp_path: Path) -> None:
    (tmp_path / "video.m4s").write_bytes(b"v")
    with pytest.raises(TranscodeError):
        FfmpegTranscoder._find_m4s_pair(tmp_path)


def test_title_from_entry_json(tmp_path: Path) -> None:
    (tmp_path / "entry.json").write_text(
        json.dumps({"title": "视频标题", "page_data": {"part": "P2"}}), encoding="utf-8"
    )
    assert FfmpegTranscoder._title(tmp_path) == "视频标题"


def test_title_falls_back_to_folder_name(tmp_path: Path) -> None:
    assert FfmpegTranscoder._title(tmp_path) == tmp_path.name


def test_has_entry_json_bounded_depth(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "entry.json").write_text("{}", encoding="utf-8")
    assert FfmpegTranscoder._has_entry_json(tmp_path)
