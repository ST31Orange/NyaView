"""Rotate Bilibili downloads onto their backup CDN hosts.

Why this exists
---------------
Bilibili's ``playurl`` API does not return one URL per stream — it returns a
*primary* URL plus a list of *backup* URLs, each pointing at a different CDN
origin (they differ in the ``os=`` query parameter and each carries its own
``upsig``, valid for ~120 minutes):

    dash.video[] / dash.audio[]  ->  baseUrl  + backupUrl[]
    durl[]                       ->  url      + backup_url[]

The **web player** falls back to a backup when the primary is slow or broken.
**yt-dlp does not**: ``extractor/bilibili.py`` reads only ``baseUrl`` (across the
whole yt-dlp codebase ``backup_url`` is used by the xiaohongshu extractor only).
A user far from the origin Bilibili assigned them — for example in Spain — can
therefore have the transfer cut short ("Downloaded N bytes, expected M bytes")
on every attempt, while the very same video plays fine in the browser.

What this does
--------------
Patches ``BilibiliBaseIE.extract_formats``. That method is defined once on the
base class and called by every Bilibili extractor (regular videos, bangumi,
courses, ...), so a single patch covers all of them. When NyaView asks for
backup #k (via ``extractor_args``), each stream's primary URL is replaced by its
k-th backup *before* yt-dlp builds the format list. The URLs are already signed,
so no extra API request is needed and nothing has to be re-signed.

The requested index travels in ``extractor_args`` — i.e. in the ``YoutubeDL``
params — so concurrent jobs never interfere with each other.
"""

from __future__ import annotations

from typing import Any, Iterable

# How the primary URL is stored on a stream, in the order yt-dlp looks them up
# (``traverse_obj(audio, 'baseUrl', 'base_url', 'url')``).
_PRIMARY_KEYS = ("baseUrl", "base_url", "url")
_BACKUP_KEYS = ("backupUrl", "backup_url")
_DASH_STREAM_KEYS = ("video", "audio")

#: Name of the ``extractor_args`` entry NyaView uses to request a backup host.
CONFIG_KEY = "cdn_backup"
EXTRACTOR_KEY = "bilibili"


def _iter_streams(play_info: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield every stream entry that carries a playable URL."""
    dash = play_info.get("dash")
    if isinstance(dash, dict):
        for key in _DASH_STREAM_KEYS:
            for item in dash.get(key) or []:
                if isinstance(item, dict):
                    yield item
        for key in ("dolby", "flac"):  # dolby.audio is a list, flac.audio a dict
            container = dash.get(key)
            if not isinstance(container, dict):
                continue
            audio = container.get("audio")
            if isinstance(audio, dict):
                yield audio
            elif isinstance(audio, list):
                for item in audio:
                    if isinstance(item, dict):
                        yield item
    for item in play_info.get("durl") or []:
        if isinstance(item, dict):
            yield item


def _backups_of(stream: dict[str, Any]) -> list[str]:
    for key in _BACKUP_KEYS:
        value = stream.get(key)
        if isinstance(value, list):
            return [str(u) for u in value if isinstance(u, str) and u]
    return []


def _apply(stream: dict[str, Any], url: str) -> None:
    touched = False
    for key in _PRIMARY_KEYS:
        if key in stream:
            stream[key] = url
            touched = True
    if not touched:  # dash entries always have baseUrl, but be defensive
        stream["baseUrl"] = url


def rotate_to_backup(play_info: dict[str, Any], index: int) -> int:
    """Point every stream at its ``index``-th backup URL (1-based).

    Returns how many streams were actually switched. ``index <= 0`` keeps the
    primary URLs (yt-dlp's normal behaviour), and a stream that has no such
    backup is left alone rather than broken.
    """
    if index <= 0 or not isinstance(play_info, dict):
        return 0
    switched = 0
    for stream in _iter_streams(play_info):
        backups = _backups_of(stream)
        if len(backups) < index:
            continue
        target = backups[index - 1]
        if not target:
            continue
        _apply(stream, target)
        switched += 1
    return switched


def backup_count(play_info: dict[str, Any]) -> int:
    """How many backup hosts this response offers (for logging/diagnostics)."""
    counts = [len(_backups_of(s)) for s in _iter_streams(play_info)]
    return max(counts) if counts else 0


# --------------------------------------------------------------------- patching

_installed = ""


def requested_index(extractor: Any) -> int:
    """Read the index NyaView asked for via ``extractor_args``."""
    getter = getattr(extractor, "_configuration_arg", None)
    if getter is None:
        return 0
    try:
        values = getter(CONFIG_KEY, ie_key=EXTRACTOR_KEY)
    except Exception:
        return 0
    if not values:
        return 0
    try:
        return max(0, int(str(values[0]).strip()))
    except (TypeError, ValueError):
        return 0


def install() -> str:
    """Patch ``BilibiliBaseIE.extract_formats`` once. Never raises.

    Returns a short status string (useful in logs). A yt-dlp that renames or
    removes the method simply leaves the patch uninstalled — the app then
    behaves exactly as before instead of breaking.
    """
    global _installed
    if _installed:
        return _installed
    try:
        from yt_dlp.extractor.bilibili import BilibiliBaseIE
    except Exception as exc:  # pragma: no cover - depends on yt-dlp layout
        _installed = f"unavailable: {exc}"
        return _installed

    original = getattr(BilibiliBaseIE, "extract_formats", None)
    if original is None:
        _installed = "skipped: extract_formats not found"
        return _installed
    if getattr(original, "_nyaview_patched", False):
        _installed = "already patched"
        return _installed

    def extract_formats(self, play_info, *args, **kwargs):
        index = requested_index(self)
        if index > 0 and isinstance(play_info, dict):
            switched = rotate_to_backup(play_info, index)
            if switched:
                self.to_screen(
                    f"[nyaview] 改用备用 CDN 源 #{index}（{switched} 条流）"
                )
        return original(self, play_info, *args, **kwargs)

    extract_formats._nyaview_patched = True  # type: ignore[attr-defined]
    extract_formats._nyaview_original = original  # type: ignore[attr-defined]
    BilibiliBaseIE.extract_formats = extract_formats
    _installed = "patched"
    return _installed


def status() -> str:
    """The result of the last :func:`install` call ('' if never called)."""
    return _installed
