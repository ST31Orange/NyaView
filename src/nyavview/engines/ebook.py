"""Image-pack -> ebook engine for comic conversion.

Supported outputs (all built with the standard library + Pillow only, so the
frozen EXE gains exactly one dependency):

* **CBZ**  — ZIP of the page images (+ optional ``ComicInfo.xml``).
* **PDF**  — hand-written minimal writer; JPEG pages are embedded losslessly
  with ``/DCTDecode`` (no re-encode), everything else is normalised through
  Pillow first. Pillow's own PDF writer was rejected because it silently
  re-encodes JPEG at quality 75 (verified ~28% smaller and lossy), and
  ``img2pdf`` was rejected because it drags in ``pikepdf`` (a heavy C++
  extension with painful PyInstaller packaging).
* **EPUB** — hand-written fixed-layout EPUB3 (``rendition:layout`` =
  ``pre-paginated``), which is what comic/manga readers expect. A
  reflowable EPUB would reflow the artwork, so it is not offered.

Page ordering uses a natural sort so ``2.jpg`` precedes ``10.jpg``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import struct
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Sequence
from xml.sax.saxutils import escape

from nyavview.core.errors import CanceledError, TranscodeError
from nyavview.core.events import ProgressEvent
from nyavview.core.settings import Settings
from nyavview.utils import safe_filename
from nyavview.utils.paths import ensure_directory, unique_path

ProgressCallback = Callable[[ProgressEvent], None]

EbookFormat = Literal["pdf", "cbz", "epub"]
OutputMode = Literal["merge", "per_folder"]

IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}
)

# JPEG quality used only when an image must be normalised (non-JPEG source or
# EXIF rotation). 95 + 4:4:4 keeps line art clean.
_NORMALISE_QUALITY = 95


# --------------------------------------------------------------------- sorting


def natural_key(value: str) -> tuple:
    """Split a name into comparable text/number chunks.

    Numbers compare numerically (so ``2.jpg`` precedes ``10.jpg``) and every
    chunk is wrapped in a ``(kind, value)`` pair so numeric and text chunks
    never compare directly — otherwise mixed names such as ``1.png`` next to
    ``第2话`` raise ``TypeError: '<' not supported between str and int``.
    """
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", value)
        if part != ""
    )


def sort_images(paths: Iterable[Path], root: Optional[Path] = None) -> list[Path]:
    """Natural-sort images by their path relative to ``root`` (deepest last)."""
    items = list(paths)

    def key(path: Path) -> tuple:
        rel = path.relative_to(root) if root is not None else Path(path.name)
        return tuple(natural_key(part) for part in rel.parts)

    return sorted(items, key=key)


def scan_images(folder: Path, *, recursive: bool = True) -> list[Path]:
    """Return every image under ``folder`` in natural page order."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise TranscodeError(f"不是有效的文件夹：{folder}")
    walker = folder.rglob("*") if recursive else folder.glob("*")
    found = [p for p in walker if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not found:
        raise TranscodeError(f"文件夹里没有找到图片：{folder}")
    return sort_images(found, folder)


def subfolder_groups(folder: Path) -> list[tuple[str, list[Path]]]:
    """Group images by immediate subfolder (one entry per chapter/folder).

    Images sitting directly in ``folder`` form a leading group named after the
    folder itself. Subfolders without images are ignored.
    """
    folder = Path(folder).expanduser()
    groups: list[tuple[str, list[Path]]] = []
    direct = [p for p in folder.glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if direct:
        groups.append((folder.name, sort_images(direct, folder)))
    for child in sorted(folder.iterdir(), key=lambda p: natural_key(p.name)):
        if not child.is_dir():
            continue
        imgs = scan_images(child, recursive=True)
        groups.append((child.name, imgs))
    if not groups:
        raise TranscodeError(f"文件夹里没有找到图片：{folder}")
    return groups


# ----------------------------------------------------------------- image utils


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.width, im.height


def _needs_rotation(path: Path) -> bool:
    """True when EXIF says the stored pixels are not upright."""
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as im:
            exif = im.getexif()
            orientation = exif.get(0x0112, 1) if exif else 1
            return orientation not in (1, None)
    except Exception:
        return False


def _normalise_to_jpeg(path: Path, *, quality: int = _NORMALISE_QUALITY) -> tuple[bytes, int, int]:
    """Convert any image to upright RGB JPEG bytes; returns (data, w, h)."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im) or im
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            background = Image.new("RGB", im.size, (255, 255, 255))
            background.paste(im, mask=im.split()[-1])
            im = background
        elif im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, subsampling=0, optimize=True)
        return buf.getvalue(), im.width, im.height


def _jpeg_dimensions(data: bytes) -> tuple[int, int, int]:
    """Parse (width, height, components) from JPEG bytes."""
    i = 2
    length = len(data)
    while i + 9 < length:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            h = struct.unpack(">H", data[i + 5 : i + 7])[0]
            w = struct.unpack(">H", data[i + 7 : i + 9])[0]
            return w, h, data[i + 9]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg_len
    raise TranscodeError("无法解析 JPEG 尺寸")


# ------------------------------------------------------------------ PDF writer


def _pdf_escape(value: str) -> str:
    return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(pages: Sequence[Path], target: Path, *, title: str = "") -> Path:
    """Write a minimal, valid PDF embedding each page losslessly when possible."""
    if not pages:
        raise TranscodeError("没有可写入的页面")

    objs: dict[int, bytes] = {}
    page_ids: list[int] = []
    next_id = 3  # 1 = Catalog, 2 = Pages
    payloads: list[tuple[Path, bytes, int, int, str]] = []

    # First pass: normalise every page into embeddable bytes (no re-encode for
    # upright JPEG, which is the overwhelmingly common comic page).
    for path in pages:
        suffix = path.suffix.lower()
        if suffix in (".jpg", ".jpeg") and not _needs_rotation(path):
            data = path.read_bytes()
            w, h, comps = _jpeg_dimensions(data)
            colour = "/DeviceGray" if comps == 1 else ("/DeviceCMYK" if comps == 4 else "/DeviceRGB")
            payloads.append((path, data, w, h, f"/DCTDecode {colour}"))
        else:
            data, w, h = _normalise_to_jpeg(path)
            payloads.append((path, data, w, h, "/DCTDecode /DeviceRGB"))

    for _path, _data, w, h, _flt in payloads:
        page_ids.append(next_id)
        next_id += 3

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()

    for idx, (_path, data, w, h, flt) in enumerate(payloads):
        pid = page_ids[idx]
        image_id, content_id = pid + 1, pid + 2
        objs[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
            f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode()
        objs[image_id] = (
            f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
            f"/ColorSpace {flt.split()[-1]} /BitsPerComponent 8 /Filter {flt.split()[0]} "
            f"/Length {len(data)} >>\nstream\n"
        ).encode() + data + b"\nendstream"
        content = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()
        objs[content_id] = f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"

    info_id = next_id
    objs[info_id] = f"<< /Title ({_pdf_escape(title)}) /Producer (NyaView) >>".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_pos = len(out)
    max_id = max(objs)
    out += f"xref\n0 {max_id + 1}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, max_id + 1):
        out += (
            f"{offsets[num]:010d} 00000 n \n".encode()
            if num in offsets
            else b"0000000000 65535 f \n"
        )
    out += (
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R /Info {info_id} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(out))
    return target


# ------------------------------------------------------------------ CBZ writer


def _comic_info_xml(title: str, page_count: int, author: str = "") -> str:
    parts = ["<?xml version=\"1.0\" encoding=\"utf-8\"?>", "<ComicInfo>"]
    if title:
        parts.append(f"<Series>{escape(title)}</Series>")
    if author:
        parts.append(f"<Writer>{escape(author)}</Writer>")
    parts.append(f"<PageCount>{page_count}</PageCount>")
    parts.append("</ComicInfo>")
    return "".join(parts)


def build_cbz(
    pages: Sequence[Path],
    target: Path,
    *,
    title: str = "",
    author: str = "",
    comic_info: bool = True,
) -> Path:
    """Zip the page images (stored, not re-compressed) into a .cbz archive."""
    if not pages:
        raise TranscodeError("没有可写入的页面")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
        for index, page in enumerate(pages, 1):
            zf.write(page, f"{index:04d}{page.suffix.lower()}")
        if comic_info:
            zf.writestr("ComicInfo.xml", _comic_info_xml(title or target.stem, len(pages), author))
    return target


# ----------------------------------------------------------------- EPUB writer

_EPUB_CONTAINER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)

_EPUB_PAGE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<!DOCTYPE html>'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
    "<head><title>第 {index} 页</title>"
    '<meta name="viewport" content="width={w}, height={h}"/>'
    "<style>html,body{{margin:0;padding:0;height:100%;background:#000;}}"
    "img{{width:100%;height:100%;object-fit:contain;}}</style></head>"
    '<body><div><img src="images/{name}" alt="第 {index} 页"/></div></body></html>'
)


def build_epub(
    pages: Sequence[Path],
    target: Path,
    *,
    title: str = "",
    author: str = "",
) -> Path:
    """Write a fixed-layout (pre-paginated) EPUB3 suitable for comics."""
    if not pages:
        raise TranscodeError("没有可写入的页面")
    book_title = title or target.stem
    identifier = f"urn:uuid:{uuid.uuid4()}"
    manifest: list[str] = []
    spine: list[str] = []

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as zf:
        # The mimetype entry must come first and stay uncompressed.
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _EPUB_CONTAINER)

        for index, page in enumerate(pages, 1):
            name = f"{index:04d}{page.suffix.lower()}"
            media_type = "image/jpeg" if name.endswith((".jpg", ".jpeg")) else f"image/{name.rsplit('.', 1)[-1]}"
            zf.write(page, f"OEBPS/images/{name}")
            manifest.append(f'<item id="img{index}" href="images/{name}" media-type="{media_type}"/>')
            try:
                width, height = _image_size(page)
            except Exception:
                width, height = 1200, 1800
            zf.writestr(
                f"OEBPS/page{index:04d}.xhtml",
                _EPUB_PAGE.format(index=index, name=name, w=width, h=height),
            )
            manifest.append(
                f'<item id="page{index}" href="page{index:04d}.xhtml" '
                'media-type="application/xhtml+xml"/>'
            )
            spine.append(f'<itemref idref="page{index}"/>')

        # Cover = first page (also referenced explicitly for readers).
        cover_meta = ""
        if pages:
            cover_meta = '<meta name="cover" content="img1"/>'

        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid" '
            'prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="bookid">{identifier}</dc:identifier>'
            f"<dc:title>{escape(book_title)}</dc:title>"
            "<dc:language>zh</dc:language>"
            f"<dc:creator>{escape(author)}</dc:creator>"
            "<dc:modified>2026-01-01T00:00:00Z</dc:modified>"
            '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>'
            '<meta name="rendition:layout">pre-paginated</meta>'
            '<meta name="rendition:orientation">auto</meta>'
            '<meta name="rendition:spread">auto</meta>'
            f"{cover_meta}"
            "</metadata>"
            f'<manifest>{"".join(manifest)}'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            "</manifest>"
            f'<spine>{"".join(spine)}</spine>'
            "</package>"
        )
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title>'
            '<meta charset="utf-8"/></head><body>'
            f'<nav epub:type="toc"><h1>{escape(book_title)}</h1><ol>'
            f'<li><a href="page0001.xhtml">{escape(book_title)}</a></li>'
            "</ol></nav></body></html>",
        )
    return target


# ---------------------------------------------------------------------- engine


def _coerce_image_path(value: Any) -> Path:
    """Accept the shapes the UI/bridge may hand us for one page.

    Normally a plain path string, but tolerate the ``{"path": ...}`` dict from
    ``scan()`` and an accidental single-element list, so a malformed payload
    fails with a clear message instead of ``Path(list)``'s cryptic TypeError.
    """
    while isinstance(value, (list, tuple)):
        if not value:
            raise TranscodeError("图片列表里出现了空项")
        value = value[0]
    if isinstance(value, dict):
        value = value.get("path")
    if not value:
        raise TranscodeError("图片列表里出现了空路径")
    return value if isinstance(value, Path) else Path(str(value))


class EbookBuilder:
    """Scans image packs and builds PDF / CBZ / EPUB ebooks."""
    def __init__(self, settings: Settings, *, on_progress: Optional[ProgressCallback] = None):
        self.settings = settings
        self._on_progress = on_progress
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

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
                kind="ebook",
                status=status,
                progress=progress,
                percent=percent,
                speed=speed,
                eta=eta,
                detail=detail,
            )
        )

    # ---------------------------------------------------------------- scanning

    def scan(self, folder: Path, *, mode: OutputMode = "merge") -> dict[str, Any]:
        """Describe an image pack: detected page order and (optionally) groups."""
        folder = Path(folder).expanduser().resolve()
        if mode == "per_folder":
            groups = []
            for name, images in subfolder_groups(folder):
                groups.append({"name": name, "images": [self._describe(p) for p in images]})
            total = sum(len(g["images"]) for g in groups)
        else:
            images = scan_images(folder)
            groups = [{"name": folder.name, "images": [self._describe(p) for p in images]}]
            total = len(images)
        return {
            "folder": str(folder),
            "mode": mode,
            "total": total,
            "groups": groups,
            "images": [img for group in groups for img in group["images"]],
        }

    @staticmethod
    def _describe(path: Path) -> dict[str, Any]:
        try:
            width, height = _image_size(path)
        except Exception:
            width = height = 0
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {
            "path": str(path),
            "name": path.name,
            "width": width,
            "height": height,
            "size": size,
        }

    def thumbnails(self, paths: Sequence[Path], *, size: int = 150) -> list[dict[str, Any]]:
        """Inline (base64) thumbnails for the reorder UI.

        The webview cannot load local ``file://`` paths, so thumbnails travel
        as data URIs — the same trick used for the download preview image.
        """
        from PIL import Image, ImageOps

        out: list[dict[str, Any]] = []
        for path in paths:
            path = Path(path)
            entry: dict[str, Any] = {"path": str(path)}
            try:
                with Image.open(path) as im:
                    im = ImageOps.exif_transpose(im) or im
                    im.thumbnail((size, size))
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=80, optimize=True)
                entry["data"] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as exc:  # keep the row even if the thumb fails
                entry["error"] = str(exc)
            out.append(entry)
        return out

    # ------------------------------------------------------------------ build

    async def build(
        self,
        groups: Sequence[dict[str, Any]],
        target_dir: Path,
        *,
        fmt: EbookFormat = "cbz",
        title: str = "",
        author: str = "",
        overwrite: bool = False,
        comic_info: bool = True,
    ) -> list[Path]:
        """Build one ebook per group; returns the written paths.

        ``groups`` is the (possibly user-reordered) ``[{name, images:[...]}]``
        structure: ``images`` entries may be dicts with ``path`` or paths.
        """
        target_dir = ensure_directory(Path(target_dir).expanduser().resolve())
        fmt = (fmt or "cbz").lower()
        if fmt not in ("pdf", "cbz", "epub"):
            raise TranscodeError(f"不支持的电子书格式：{fmt}")

        results: list[Path] = []
        total_groups = max(1, len(groups))
        for index, group in enumerate(groups, 1):
            if self._cancel:
                raise CanceledError("ebook build cancelled by user")
            pages = [_coerce_image_path(img) for img in group.get("images", [])]
            if not pages:
                continue
            missing = [p for p in pages if not p.is_file()]
            if missing:
                raise TranscodeError(f"图片不存在：{missing[0]}")
            # Naming: with several groups ("one book per subfolder") each book
            # must be distinguishable — use the group name, optionally prefixed
            # by the user's title, instead of giving every book the same name.
            group_name = str(group.get("name") or "").strip()
            if len(groups) > 1:
                if title and group_name:
                    book_title = f"{title} - {group_name}"
                else:
                    book_title = group_name or title or target_dir.name or "ebook"
            else:
                book_title = title or group_name or target_dir.name or "ebook"
            stem = safe_filename(book_title)
            target = target_dir / f"{stem}.{fmt}"
            if not overwrite and target.exists():
                target = unique_path(target)

            self._emit(
                "running",
                progress=(index - 1) / total_groups,
                percent=(index - 1) / total_groups * 100,
                detail=f"正在生成 {target.name}（{len(pages)} 页）",
            )
            # The writers are CPU/IO bound; keep the event loop responsive.
            await asyncio.to_thread(
                self._write_one, pages, target, fmt, book_title, author, comic_info
            )
            results.append(target)
            self._emit(
                "running",
                progress=index / total_groups,
                percent=index / total_groups * 100,
                detail=f"已生成 {target.name}",
            )

        if not results:
            raise TranscodeError("没有可转换的图片")
        self._emit("finished", progress=1.0, percent=100.0, detail=f"完成，共 {len(results)} 个文件")
        return results

    def _write_one(
        self,
        pages: Sequence[Path],
        target: Path,
        fmt: str,
        title: str,
        author: str,
        comic_info: bool,
    ) -> Path:
        """Write to a temp file then rename, so a cancel never leaves a stub."""
        import os

        temp = target.with_name(f".{target.name}.part-{os.getpid()}")
        try:
            if fmt == "pdf":
                build_pdf(pages, temp, title=title)
            elif fmt == "cbz":
                build_cbz(pages, temp, title=title, author=author, comic_info=comic_info)
            else:
                build_epub(pages, temp, title=title, author=author)
            os.replace(temp, target)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        return target
