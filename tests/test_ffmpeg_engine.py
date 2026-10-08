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


def test_build_args_max_compatibility() -> None:
    """Output must be forced to 8-bit 4:2:0 High profile so old players
    (QQ影音) accept it — 10-bit/4:4:4 sources would break them."""
    args = _engine()._build_args(
        [Path("a.mkv")], Path("o.mp4"), audio_mode="encode", overwrite=False
    )
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-profile:v") + 1] == "high"
    assert args[args.index("-crf") + 1] == "23"  # default standard quality


def test_build_args_uses_video_crf() -> None:
    engine = FfmpegTranscoder(Settings(ffmpeg_path=Path("ffmpeg"), video_crf=18))
    args = engine._build_args([Path("a.mkv")], Path("o.mp4"), audio_mode="encode", overwrite=False)
    assert args[args.index("-crf") + 1] == "18"


def _write_cache(folder: Path, title: str = "T") -> Path:
    """Create a minimal valid Bilibili cache folder (entry.json + m4s pair)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "entry.json").write_text(json.dumps({"title": title}), encoding="utf-8")
    (folder / "video.m4s").write_bytes(b"v")
    (folder / "audio.m4s").write_bytes(b"a")
    return folder


def test_cache_roots_detects_direct_root(tmp_path: Path) -> None:
    _write_cache(tmp_path)
    engine = _engine()
    assert engine.cache_roots(tmp_path) == [tmp_path]


def test_cache_roots_treats_parent_as_root_when_nested(tmp_path: Path) -> None:
    """A parent containing cache folders converts EACH child that directly
    holds entry.json (multi-cache one-pass conversion)."""
    for name in ("one", "two"):
        _write_cache(tmp_path / name)
    (tmp_path / "notacache").mkdir()
    engine = _engine()
    assert engine.cache_roots(tmp_path) == [tmp_path / "one", tmp_path / "two"]


def test_cache_roots_recursive_wrapped_levels(tmp_path: Path) -> None:
    """Caches nested one or two folders deeper are still found (recursive
    scan) — the exact case the user reported as unrecognized."""
    inner = _write_cache(tmp_path / "outer" / "缓存A")
    engine = _engine()
    assert engine.cache_roots(tmp_path) == [inner]


def test_cache_roots_recursive_deep_nesting(tmp_path: Path) -> None:
    deep = _write_cache(tmp_path / "a" / "b" / "c" / "缓存")
    engine = _engine()
    assert engine.cache_roots(tmp_path) == [deep]


def test_cache_roots_mixed_depths_finds_all(tmp_path: Path) -> None:
    """Caches at different depths are ALL collected (不遗漏)."""
    l1 = _write_cache(tmp_path / "直接")
    l3 = _write_cache(tmp_path / "x" / "y" / "深层")
    engine = _engine()
    assert set(engine.cache_roots(tmp_path)) == {l1, l3}


def test_cache_roots_multip_skips_metadata_parent(tmp_path: Path) -> None:
    """Multi-P: a parent with entry.json but no own media is skipped, while
    each per-P subfolder (entry.json + media) is converted — no duplicate."""
    parent = tmp_path / "aid"
    parent.mkdir()
    (parent / "entry.json").write_text(json.dumps({"title": "合集"}), encoding="utf-8")
    p1 = _write_cache(parent / "cid1", "P1")
    p2 = _write_cache(parent / "cid2", "P2")
    engine = _engine()
    assert set(engine.cache_roots(tmp_path)) == {p1, p2}


def test_cache_roots_real_layout_wrapped(tmp_path: Path) -> None:
    """Real client layout (entry.json + 64/video.m4s) wrapped one level deep
    is found (the user's scenario ⑤)."""
    cache = tmp_path / "downloads" / "12345"
    cache.mkdir(parents=True)
    (cache / "entry.json").write_text('{"title":"T"}', encoding="utf-8")
    (cache / "64").mkdir()
    (cache / "64" / "video.m4s").write_bytes(b"v")
    (cache / "64" / "audio.m4s").write_bytes(b"a")
    engine = _engine()
    assert engine.cache_roots(tmp_path) == [cache]


def test_cache_roots_ignores_entry_json_without_media(tmp_path: Path) -> None:
    """A folder with only entry.json (no m4s) is not a convertible root."""
    only = tmp_path / "meta"
    only.mkdir()
    (only / "entry.json").write_text("{}", encoding="utf-8")
    with pytest.raises(TranscodeError):
        _engine().cache_roots(tmp_path)


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

