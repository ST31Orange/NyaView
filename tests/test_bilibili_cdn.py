"""Tests for the Bilibili backup-CDN rotation.

Bilibili's playurl API returns a primary URL plus backup URLs on *different*
CDN origins (they differ in the ``os=`` parameter). The web player falls back
between them; yt-dlp uses ``baseUrl`` only, which is why a user far from the
assigned origin (e.g. in Spain) can see every attempt cut short while the same
video plays fine in the browser.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from nyavview.core.settings import Settings
from nyavview.engines import bilibili_cdn
from nyavview.engines.ytdlp import YtDlpEngine

# Mirrors the real response shape (dash.video/dash.audio -> baseUrl + backupUrl,
# durl -> url + backup_url). Hosts are the ones Bilibili actually hands out.
PLAY_INFO = {
    "dash": {
        "video": [
            {
                "id": 80,
                "baseUrl": "https://upos-sz-mirrorks3.bilivideo.com/v80?os=ks3bv&upsig=A",
                "backupUrl": [
                    "https://upos-sz-mirrorks3c.bilivideo.com/v80?os=ks3cbv&upsig=B",
                    "https://upos-sz-mirrorali.bilivideo.com/v80?os=alibv&upsig=C",
                ],
            },
            {
                "id": 64,
                "baseUrl": "https://upos-sz-mirrorks3.bilivideo.com/v64?os=ks3bv",
                "backupUrl": ["https://upos-sz-mirrorhw.bilivideo.com/v64?os=hwvb"],
            },
        ],
        "audio": [
            {
                "id": 30280,
                "baseUrl": "https://upos-sz-mirrorks3.bilivideo.com/a1?os=ks3bv",
                "backupUrl": [
                    "https://upos-sz-mirrorks3c.bilivideo.com/a1?os=ks3cbv",
                    "https://upos-sz-mirrorali.bilivideo.com/a1?os=alibv",
                ],
            },
            {"id": 30232, "baseUrl": "https://upos-sz-mirrorks3.bilivideo.com/a2?os=ks3bv"},
        ],
        "dolby": {"audio": {"baseUrl": "https://x/d?os=d", "backupUrl": ["https://x/d-b1?os=db"]}},
        "flac": {"audio": {"baseUrl": "https://x/f?os=f"}},
    },
    "durl": [{"url": "https://x/seg1?os=s", "backup_url": ["https://y/seg1-b1?os=sb"]}],
}

V80 = "https://upos-sz-mirrorks3.bilivideo.com/v80?os=ks3bv&upsig=A"
V80_B1 = "https://upos-sz-mirrorks3c.bilivideo.com/v80?os=ks3cbv&upsig=B"
V80_B2 = "https://upos-sz-mirrorali.bilivideo.com/v80?os=alibv&upsig=C"


# ------------------------------------------------------------------ rotation


def test_index_zero_keeps_the_primary() -> None:
    info = deepcopy(PLAY_INFO)
    assert bilibili_cdn.rotate_to_backup(info, 0) == 0
    assert info["dash"]["video"][0]["baseUrl"] == V80
    assert info["durl"][0]["url"] == "https://x/seg1?os=s"


def test_negative_index_keeps_the_primary() -> None:
    info = deepcopy(PLAY_INFO)
    assert bilibili_cdn.rotate_to_backup(info, -1) == 0
    assert info["dash"]["video"][0]["baseUrl"] == V80


def test_first_backup_is_used_for_every_stream_that_has_one() -> None:
    info = deepcopy(PLAY_INFO)
    # video[0], video[1], audio[0], dolby.audio, durl[0] have backups;
    # audio[1] and flac.audio do not
    assert bilibili_cdn.rotate_to_backup(info, 1) == 5
    assert info["dash"]["video"][0]["baseUrl"] == V80_B1
    assert info["dash"]["video"][1]["baseUrl"] == "https://upos-sz-mirrorhw.bilivideo.com/v64?os=hwvb"
    assert info["dash"]["audio"][0]["baseUrl"] == "https://upos-sz-mirrorks3c.bilivideo.com/a1?os=ks3cbv"
    assert info["dash"]["dolby"]["audio"]["baseUrl"] == "https://x/d-b1?os=db"
    assert info["durl"][0]["url"] == "https://y/seg1-b1?os=sb"
    # streams without a backup stay exactly as they were
    assert info["dash"]["audio"][1]["baseUrl"] == "https://upos-sz-mirrorks3.bilivideo.com/a2?os=ks3bv"
    assert info["dash"]["flac"]["audio"]["baseUrl"] == "https://x/f?os=f"


def test_second_backup_only_moves_streams_that_have_two() -> None:
    info = deepcopy(PLAY_INFO)
    # only video[0], audio[0] have two backups
    assert bilibili_cdn.rotate_to_backup(info, 2) == 2
    assert info["dash"]["video"][0]["baseUrl"] == V80_B2
    assert info["dash"]["audio"][0]["baseUrl"] == "https://upos-sz-mirrorali.bilivideo.com/a1?os=alibv"
    # a stream with only one backup must NOT be broken by asking for the 2nd
    assert info["dash"]["video"][1]["baseUrl"] == "https://upos-sz-mirrorks3.bilivideo.com/v64?os=ks3bv"


def test_out_of_range_index_changes_nothing() -> None:
    info = deepcopy(PLAY_INFO)
    assert bilibili_cdn.rotate_to_backup(info, 9) == 0
    assert info["dash"]["video"][0]["baseUrl"] == V80


def test_backup_count_reports_the_richest_stream() -> None:
    assert bilibili_cdn.backup_count(PLAY_INFO) == 2
    assert bilibili_cdn.backup_count({"durl": [{"url": "u", "backup_url": ["a"]}]}) == 1
    assert bilibili_cdn.backup_count({}) == 0


def test_snake_case_fields_are_supported() -> None:
    info = {"dash": {"video": [{"base_url": "u1", "backup_url": ["u2"]}]}}
    assert bilibili_cdn.rotate_to_backup(info, 1) == 1
    assert info["dash"]["video"][0]["base_url"] == "u2"


def test_malformed_input_is_tolerated() -> None:
    assert bilibili_cdn.rotate_to_backup({}, 1) == 0
    assert bilibili_cdn.rotate_to_backup({"dash": None, "durl": None}, 1) == 0
    assert bilibili_cdn.rotate_to_backup({"dash": {"video": [None, 3, "x"]}}, 1) == 0


# --------------------------------------------------------------------- patch


class _FakeExtractor:
    """Minimal stand-in: only what the patched method touches."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.screen: list[str] = []

    def _configuration_arg(self, key, ie_key=None):
        return [str(self.index)] if self.index else []

    def to_screen(self, message, *args, **kwargs) -> None:
        self.screen.append(str(message))


def _call_patched(extractor, play_info):
    """Call the patched method; the real body may fail on our fake extractor,
    we only care that the URL rewrite happened before it was reached."""
    from yt_dlp.extractor.bilibili import BilibiliBaseIE

    try:
        BilibiliBaseIE.extract_formats(extractor, play_info)
    except Exception:
        pass


def test_patch_installs_once() -> None:
    assert bilibili_cdn.install() in ("patched", "already patched")
    assert bilibili_cdn.status() in ("patched", "already patched")
    from yt_dlp.extractor.bilibili import BilibiliBaseIE

    assert getattr(BilibiliBaseIE.extract_formats, "_nyaview_patched", False)


def test_patch_rotates_and_logs_when_a_backup_is_requested() -> None:
    bilibili_cdn.install()
    info = deepcopy(PLAY_INFO)
    extractor = _FakeExtractor(1)
    _call_patched(extractor, info)
    assert info["dash"]["video"][0]["baseUrl"] == V80_B1
    assert any("备用 CDN" in line for line in extractor.screen)


def test_patch_leaves_everything_alone_without_the_config_arg() -> None:
    bilibili_cdn.install()
    info = deepcopy(PLAY_INFO)
    extractor = _FakeExtractor(0)
    _call_patched(extractor, info)
    assert info == PLAY_INFO
    assert extractor.screen == []


# ---------------------------------------------------------------------- opts


def test_opts_pass_the_backup_index_through_extractor_args() -> None:
    engine = YtDlpEngine(Settings())
    destination = Path("downloads")

    plain = engine._opts(destination, audio_only=False, overwrite=False)
    assert "extractor_args" not in plain

    retry = engine._opts(destination, audio_only=False, overwrite=False, cdn_backup_index=2)
    assert retry["extractor_args"] == {"bilibili": {"cdn_backup": ["2"]}}


# --------------------------------------------------- integration with yt-dlp

PRIMARY_HOST = "upos-sz-mirrorks3.bilivideo.com"
BACKUP1_HOST = "upos-sz-mirrorks3c.bilivideo.com"
BACKUP2_HOST = "upos-sz-mirrorali.bilivideo.com"


def _full_play_info() -> dict:
    """A complete playurl response, so the REAL extract_formats can run."""
    p = f"https://{PRIMARY_HOST}/v?os=ks3bv&upsig=A"
    b1 = f"https://{BACKUP1_HOST}/v?os=ks3cbv&upsig=B"
    b2 = f"https://{BACKUP2_HOST}/v?os=alibv&upsig=C"
    return {
        "quality": 80,
        "timelength": 60000,
        "accept_quality": [80, 64],
        "support_formats": [
            {"quality": 80, "new_description": "1080P", "format": "flv"},
            {"quality": 64, "new_description": "720P", "format": "flv720"},
        ],
        "dash": {
            "duration": 60,
            "video": [
                {"id": 80, "baseUrl": p + "&k=80", "backupUrl": [b1 + "&k=80", b2 + "&k=80"],
                 "width": 1920, "height": 1080, "bandwidth": 2000000,
                 "codecs": "avc1.640028", "mimeType": "video/mp4", "size": 50910358},
                {"id": 64, "baseUrl": p + "&k=64", "backupUrl": [b1 + "&k=64"],
                 "width": 1280, "height": 720, "bandwidth": 1000000,
                 "codecs": "avc1.64001f", "mimeType": "video/mp4", "size": 20000000},
            ],
            "audio": [
                {"id": 30280, "baseUrl": p + "&a=1", "backupUrl": [b1 + "&a=1", b2 + "&a=1"],
                 "bandwidth": 128000, "codecs": "mp4a.40.2", "mimeType": "audio/mp4"},
            ],
        },
    }


def _real_extractor(index: int):
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.bilibili import BiliBiliIE

    opts: dict = {"quiet": True, "no_warnings": True}
    if index:
        opts["extractor_args"] = {"bilibili": {"cdn_backup": [str(index)]}}
    return BiliBiliIE(YoutubeDL(opts))


def test_real_extractor_switches_host_on_retry() -> None:
    """End-to-end through yt-dlp's own format builder."""
    bilibili_cdn.install()

    def hosts(index: int) -> set[str]:
        formats = _real_extractor(index).extract_formats(_full_play_info())
        return {f["url"].split("/")[2] for f in formats}

    assert hosts(0) == {PRIMARY_HOST}
    assert hosts(1) == {BACKUP1_HOST}
    # the 720P stream only has one backup, so it stays on the primary rather
    # than being pointed at a host that does not exist for it
    assert hosts(2) == {BACKUP2_HOST, PRIMARY_HOST}


def test_every_bilibili_subclass_reads_the_config() -> None:
    """The arg is looked up under 'bilibili' even though the subclasses report a
    different ie_key() — without the explicit ie_key this silently did nothing."""
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.bilibili import (
        BiliBiliBangumiIE,
        BiliBiliIE,
        BilibiliCheeseIE,
    )

    ydl = YoutubeDL({"quiet": True, "no_warnings": True,
                     "extractor_args": {"bilibili": {"cdn_backup": ["2"]}}})
    for cls in (BiliBiliIE, BiliBiliBangumiIE, BilibiliCheeseIE):
        assert bilibili_cdn.requested_index(cls(ydl)) == 2, cls.__name__
    # and no config means "keep the primary"
    assert bilibili_cdn.requested_index(BiliBiliIE(YoutubeDL({"quiet": True}))) == 0
