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
from dataclasses import dataclass, field
from html import escape as html_escape
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

# Sources accepted by the "merge into one book" feature.
BOOK_EXTS = frozenset({".epub", ".cbz", ".pdf", ".txt"})

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".avif": "image/avif",
}

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


class _PdfWriter:
    """Tiny PDF object collector so objects can be added in any order."""

    def __init__(self) -> None:
        self.objs: dict[int, bytes] = {}
        self.next_id = 1

    def reserve(self) -> int:
        num = self.next_id
        self.next_id += 1
        return num

    def add(self, body: bytes) -> int:
        num = self.reserve()
        self.objs[num] = body
        return num

    def set(self, num: int, body: bytes) -> None:
        self.objs[num] = body

    def write(self, target: Path, *, info_id: int = 0) -> Path:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for num in sorted(self.objs):
            offsets[num] = len(out)
            out += f"{num} 0 obj\n".encode() + self.objs[num] + b"\nendobj\n"
        xref_pos = len(out)
        max_id = max(self.objs)
        out += f"xref\n0 {max_id + 1}\n".encode() + b"0000000000 65535 f \n"
        for num in range(1, max_id + 1):
            out += (
                f"{offsets[num]:010d} 00000 n \n".encode()
                if num in offsets
                else b"0000000000 65535 f \n"
            )
        info_ref = f" /Info {info_id} 0 R" if info_id else ""
        out += (
            f"trailer\n<< /Size {max_id + 1} /Root 1 0 R{info_ref} >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(out))
        return target


def _write_pdf(
    payloads: Sequence[tuple[bytes, int, int, str, str]],
    target: Path,
    *,
    title: str = "",
    author: str = "",
    outline: Sequence[SourceToc] = (),
) -> Path:
    """Write a PDF from (data, width, height, colours, filter) payloads.

    ``outline`` becomes the PDF bookmark tree, which is how a PDF carries a
    table of contents.
    """
    if not payloads:
        raise TranscodeError("没有可写入的页面")
    pdf = _PdfWriter()
    catalog_id = pdf.reserve()  # 1
    pages_id = pdf.reserve()  # 2
    info_id = pdf.reserve()  # 3
    outlines_id = pdf.reserve() if outline else 0

    page_ids: list[int] = []
    for data, w, h, colour, flt in payloads:
        page_id = pdf.reserve()
        image_id = pdf.reserve()
        content_id = pdf.reserve()
        page_ids.append(page_id)
        pdf.set(
            page_id,
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {w} {h}] "
                f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode(),
        )
        pdf.set(
            image_id,
            (
                f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                f"/ColorSpace {colour} /BitsPerComponent 8 /Filter {flt} "
                f"/Length {len(data)} >>\nstream\n"
            ).encode()
            + data
            + b"\nendstream",
        )
        content = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()
        pdf.set(content_id, f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    pdf.set(pages_id, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())

    def build_outline(items: Sequence[SourceToc], parent_id: int) -> tuple[int, int, int]:
        """Returns (first_id, last_id, total_count)."""
        ids: list[int] = []
        counts: list[int] = []
        for item in items:
            node_id = pdf.reserve()
            ids.append(node_id)
            child_first = child_last = child_count = 0
            if item.children:
                child_first, child_last, child_count = build_outline(item.children, node_id)
            page = page_ids[min(max(item.page_index, 0), len(page_ids) - 1)]
            parts = [
                f"/Title ({_pdf_escape(item.title)})",
                f"/Parent {parent_id} 0 R",
                f"/Dest [{page} 0 R /Fit]",
            ]
            if child_first:
                parts += [f"/First {child_first} 0 R", f"/Last {child_last} 0 R"]
            parts.append(f"/Count {child_count if child_count else 0}")
            if len(ids) > 1:
                parts.append(f"/Prev {ids[-2]} 0 R")
            pdf.set(node_id, ("<< " + " ".join(parts) + " >>").encode())
            counts.append(child_count)
        # Link /Next now that every sibling id is known.
        for pos, node_id in enumerate(ids):
            body = pdf.objs[node_id]
            if pos + 1 < len(ids):
                body = body[:-3] + f" /Next {ids[pos + 1]} 0 R >>".encode()
                pdf.set(node_id, body)
        total = len(ids) + sum(counts)
        return (ids[0], ids[-1], total) if ids else (0, 0, 0)

    outline_ref = b""
    if outline:
        first, last, count = build_outline(outline, outlines_id)
        pdf.set(
            outlines_id,
            f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {count} >>".encode(),
        )
        outline_ref = f" /Outlines {outlines_id} 0 R".encode()

    pdf.set(catalog_id, b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R" + outline_ref + b" >>")
    info = f"<< /Title ({_pdf_escape(title)}) /Producer (NyaView)"
    if author:
        info += f" /Author ({_pdf_escape(author)})"
    pdf.set(info_id, (info + " >>").encode())
    return pdf.write(target, info_id=info_id)


def _pdf_payload_for_file(path: Path) -> tuple[bytes, int, int, str, str]:
    """Embed a page losslessly when it is already an upright JPEG."""
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg") and not _needs_rotation(path):
        data = path.read_bytes()
        w, h, comps = _jpeg_dimensions(data)
        colour = "/DeviceGray" if comps == 1 else ("/DeviceCMYK" if comps == 4 else "/DeviceRGB")
        return data, w, h, colour, "/DCTDecode"
    data, w, h = _normalise_to_jpeg(path)
    return data, w, h, "/DeviceRGB", "/DCTDecode"


def build_pdf(pages: Sequence[Path], target: Path, *, title: str = "") -> Path:
    """Write a minimal, valid PDF embedding each page losslessly when possible."""
    if not pages:
        raise TranscodeError("没有可写入的页面")
    payloads = [_pdf_payload_for_file(Path(page)) for page in pages]
    return _write_pdf(payloads, target, title=title)


def build_pdf_from_images(
    images: Sequence[tuple[str, bytes, int, int]],
    target: Path,
    *,
    title: str = "",
    author: str = "",
    outline: Sequence[SourceToc] = (),
) -> Path:
    """Build a PDF from in-memory images, with an optional bookmark tree."""
    payloads: list[tuple[bytes, int, int, str, str]] = []
    for _name, data, w, h, in images:
        if data.startswith(b"\xff\xd8"):
            try:
                real_w, real_h, comps = _jpeg_dimensions(data)
                w, h = real_w, real_h
                colour = "/DeviceGray" if comps == 1 else (
                    "/DeviceCMYK" if comps == 4 else "/DeviceRGB"
                )
            except Exception:
                colour = "/DeviceRGB"
        else:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                rgb = im.convert("RGB")
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=_NORMALISE_QUALITY, subsampling=0)
                data = buf.getvalue()
                w, h = rgb.size
            colour = "/DeviceRGB"
        payloads.append((data, w, h, colour, "/DCTDecode"))
    return _write_pdf(payloads, target, title=title, author=author, outline=outline)


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

# Fixed-layout page. The image goes inside an <svg> with an exact viewBox so the
# document's intrinsic size is exactly one page — a reading system that paginates
# reflowably then cannot split it into a real page plus a blank one.
_EPUB_PAGE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<!DOCTYPE html>'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" lang="zh">'
    "<head><title>{title}</title><meta charset=\"utf-8\"/>"
    '<meta name="viewport" content="width={w}, height={h}"/>'
    "<style>html,body{{margin:0;padding:0;width:100%;height:100%;overflow:hidden;"
    "background:#fff;}}svg{{display:block;width:100%;height:100%;}}</style></head>"
    '<body><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
    'viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" width="100%" height="100%">'
    '<image xlink:href="{src}" width="{w}" height="{h}"/></svg></body></html>'
)

_EPUB_TEXT_CSS = (
    "html,body{margin:0;padding:0;}"
    "body{line-height:1.75;font-family:serif;padding:0.6em 0.9em;}"
    "h1,h2{font-size:1.25em;margin:1.1em 0 0.7em;line-height:1.4;}"
    "h2{font-size:1.1em;}"
    "p{margin:0 0 0.85em;text-indent:2em;}"
    "p.no-indent{text-indent:0;}"
)

_EPUB_TEXT_PAGE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<!DOCTYPE html>'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh">'
    "<head><title>{title}</title><meta charset=\"utf-8\"/>"
    "<style>{css}</style></head><body>{body}</body></html>"
)


@dataclass
class EpubDoc:
    """One spine document (a fixed-layout page or a reflowable chapter)."""

    title: str
    body: str  # the complete XHTML file
    layout: str = "pre-paginated"  # or "reflowable"
    assets: list[tuple[str, bytes, str]] = field(default_factory=list)  # href, data, media
    cover: bool = False


@dataclass
class EpubTocEntry:
    title: str
    href: str
    children: list["EpubTocEntry"] = field(default_factory=list)


def _image_media_type(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "image/jpeg")


def _toc_html(entries: Sequence[EpubTocEntry]) -> str:
    if not entries:
        return ""
    items = "".join(
        f'<li><a href="{escape(e.href)}">{escape(e.title)}</a>{_toc_html(e.children)}</li>'
        for e in entries
    )
    return f"<ol>{items}</ol>"


def write_epub(
    target: Path,
    *,
    title: str,
    author: str = "",
    language: str = "zh",
    docs: Sequence[EpubDoc],
    toc: Sequence[EpubTocEntry] = (),
) -> Path:
    """Write an EPUB3 package from pre-built spine documents.

    ``rendition:*`` metadata must use ``property=`` (not ``name=``) for reading
    systems to honour it — with the EPUB2-style ``name=`` form the book is
    treated as reflowable and a paginating reader splits each page, producing
    the blank pages users saw.
    """
    if not docs:
        raise TranscodeError("没有可写入的内容")
    book_title = title or target.stem
    identifier = f"urn:uuid:{uuid.uuid4()}"
    # The package default; each itemref may override it.
    package_layout = "reflowable" if all(d.layout == "reflowable" for d in docs) else "pre-paginated"

    manifest: list[str] = []
    spine: list[str] = []
    cover_id: Optional[str] = None

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as zf:
        # The mimetype entry must come first and stay uncompressed.
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _EPUB_CONTAINER)

        for index, doc in enumerate(docs, 1):
            for asset_no, (href, data, media) in enumerate(doc.assets, 1):
                zf.writestr(f"OEBPS/{href}", data)
                asset_id = f"a{index:04d}_{asset_no}"
                props = ""
                if doc.cover and asset_no == 1 and media.startswith("image/"):
                    props = ' properties="cover-image"'
                    cover_id = asset_id
                manifest.append(
                    f'<item id="{asset_id}" href="{escape(href)}" media-type="{media}"{props}/>'
                )
            page_name = f"page{index:04d}.xhtml"
            zf.writestr(f"OEBPS/{page_name}", doc.body)
            manifest.append(
                f'<item id="page{index:04d}" href="{page_name}" '
                'media-type="application/xhtml+xml"/>'
            )
            props = (
                ""
                if doc.layout == "reflowable"
                else ' properties="rendition:layout-pre-paginated"'
            )
            spine.append(f'<itemref idref="page{index:04d}"{props}/>')

        creator = f"<dc:creator>{escape(author)}</dc:creator>" if author else ""
        cover_meta = f'<meta name="cover" content="{cover_id}"/>' if cover_id else ""
        modified = "2026-01-01T00:00:00Z"
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid" '
            'prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="bookid">{identifier}</dc:identifier>'
            f"<dc:title>{escape(book_title)}</dc:title>"
            f"<dc:language>{escape(language)}</dc:language>"
            f"{creator}"
            f'<meta property="dcterms:modified">{modified}</meta>'
            f'<meta property="rendition:layout">{package_layout}</meta>'
            '<meta property="rendition:orientation">auto</meta>'
            # "none": never force two-page spreads (that makes readers insert
            # blank pages to align them).
            '<meta property="rendition:spread">none</meta>'
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
            'xmlns:epub="http://www.idpf.org/2007/ops" lang="zh"><head><title>目录</title>'
            '<meta charset="utf-8"/></head><body>'
            '<nav epub:type="toc" id="toc">'
            f"<h1>{escape(book_title)}</h1>{_toc_html(toc)}"
            "</nav></body></html>",
        )
    return target


def build_epub(
    pages: Sequence[Path],
    target: Path,
    *,
    title: str = "",
    author: str = "",
    toc: Optional[Sequence[EpubTocEntry]] = None,
) -> Path:
    """Fixed-layout (pre-paginated) EPUB3 for a comic: one image per page."""
    if not pages:
        raise TranscodeError("没有可写入的页面")
    book_title = title or target.stem
    docs: list[EpubDoc] = []
    for index, page in enumerate(pages, 1):
        name = f"{index:04d}{page.suffix.lower()}"
        try:
            width, height = _image_size(page)
        except Exception:
            width, height = 1200, 1800
        page_title = f"第 {index} 页"
        docs.append(
            EpubDoc(
                title=page_title,
                body=_EPUB_PAGE.format(
                    title=page_title, w=width, h=height, src=f"images/{name}"
                ),
                assets=[(f"images/{name}", page.read_bytes(), _image_media_type(name))],
                cover=(index == 1),
            )
        )
    if toc is None:
        toc = [EpubTocEntry(book_title, "page0001.xhtml")]
    return write_epub(target, title=book_title, author=author, docs=docs, toc=toc)


# ------------------------------------------------------- source book readers


@dataclass
class SourceToc:
    """A table-of-contents entry of a source book (page index, 0-based)."""

    title: str
    page_index: int
    children: list["SourceToc"] = field(default_factory=list)


@dataclass
class BookSource:
    """A parsed source book, ready to be merged into a collection."""

    path: Path
    title: str
    kind: str  # epub | cbz | pdf | txt
    images: list[tuple[str, bytes, int, int]] = field(default_factory=list)  # name, data, w, h
    chapters: list[tuple[str, list[str]]] = field(default_factory=list)  # title, paragraphs
    toc: list[SourceToc] = field(default_factory=list)
    note: str = ""  # extra info surfaced in the UI/log


# CJK novels are very often GB18030/GBK rather than UTF-8, and gb18030 can decode
# almost any byte string, so UTF-8 must be attempted first.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-16", "gb18030", "big5", "cp1252")

_CHAPTER_PATTERNS = (
    # "第一章" / "第1章 开端" / "第 12 节" / "第三卷"
    # A separator must follow the marker, otherwise prose such as
    # "第一章说的是另一个故事…" would be mistaken for a heading.
    re.compile(
        r"^第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节節回卷篇部集话話]"
        r"(?:[\s、：:·\-—]+[^\u3002\uff01\uff1f\uff0c\uff1b]{1,24})?$"
    ),
    re.compile(r"^(?:Chapter|CHAPTER|Ch\.|chapter)\s*\d+[\s：:.\-]{0,3}[^\u3002\uff01\uff1f\uff0c]{0,24}$"),
    re.compile(r"^(?:序章|序言|自序|序|前言|引子|楔子|后记|後記|尾声|尾聲|终章|終章|番外.{0,20}|外传.{0,20})$"),
    # Roman-numeral section headings, e.g. "I.粘土中的恐怖" / "II. 风暴中的过客"
    re.compile(r"^[IVXLC]{1,6}\s*[.、．]\s*[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9 ·\-]{0,18}$"),
    # Chinese-numeral headings, e.g. "一、初见"
    re.compile(r"^[一二三四五六七八九十百]+\s*[、.．]\s*[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9 ·\-]{0,18}$"),
)
_CHAPTER_MAX_LEN = 60
# A heading-less work stays one chapter; only split when it is really huge, so
# the TOC does not fill up with generic part names.
_MAX_CHARS_PER_PART = 120_000


def read_text_file(path: Path) -> str:
    """Read a text file, guessing the encoding (UTF-8 first, then GB18030/Big5)."""
    raw = Path(path).read_bytes()
    for encoding in _TEXT_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # gb18030/big5 never fail, so reject decodings full of replacement chars.
        if text.count("\ufffd") <= max(1, len(text) // 500):
            return text.replace("\r\n", "\n").replace("\r", "\n")
    return raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def is_chapter_heading(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > _CHAPTER_MAX_LEN:
        return False
    return any(pattern.match(text) for pattern in _CHAPTER_PATTERNS)


def split_text_chapters(
    text: str,
    *,
    book_title: str = "",
    max_chars_per_part: int = _MAX_CHARS_PER_PART,
) -> list[tuple[str, list[str]]]:
    """Split a novel into (chapter title, paragraphs).

    When the file carries no recognisable headings the whole work becomes ONE
    chapter named after the book — not a run of generic "第 N 部分" entries,
    which used to make a merged collection's TOC a wall of identical names.
    Only a very long heading-less work is split, and its parts are named
    ``书名（1/N）`` so they stay distinguishable.
    """
    lines = text.split("\n")
    chapters: list[tuple[str, list[str]]] = []
    current_title = ""
    buffer: list[str] = []
    found_heading = False

    def flush() -> None:
        if not buffer:
            return
        paras = [p.strip() for p in "\n".join(buffer).split("\n") if p.strip()]
        if not paras:
            return
        # Text before the first heading (or the whole file) is titled with the
        # book name when it has no heading of its own.
        chapters.append((current_title or book_title or "正文", paras))

    for line in lines:
        if is_chapter_heading(line):
            flush()
            buffer = []
            found_heading = True
            current_title = line.strip()
        else:
            buffer.append(line)
    flush()

    if found_heading:
        # Text before the first heading is a preface (title page / epigraph /
        # translator note), not the book itself — naming it after the book would
        # repeat the book's own TOC entry.
        if len(chapters) > 1 and chapters[0][0] == (book_title or "正文"):
            chapters[0] = ("前言", chapters[0][1])
        return chapters

    # No headings at all.
    stripped = [p.strip() for p in text.split("\n") if p.strip()]
    if not stripped:
        return []
    label = book_title or "正文"
    if sum(len(p) for p in stripped) <= max_chars_per_part:
        return [(label, stripped)]

    parts: list[list[str]] = []
    chunk: list[str] = []
    size = 0
    for para in stripped:
        chunk.append(para)
        size += len(para)
        if size >= max_chars_per_part:
            parts.append(chunk)
            chunk = []
            size = 0
    if chunk:
        parts.append(chunk)
    total = len(parts)
    return [(f"{label}（{i}/{total}）", c) for i, c in enumerate(parts, 1)]


def read_txt_source(path: Path) -> BookSource:
    path = Path(path)
    # The file name is the book title, and is used to title chapters that have
    # no heading of their own.
    chapters = split_text_chapters(read_text_file(path), book_title=path.stem)
    if not chapters:
        raise TranscodeError(f"{path.name} 里没有可用的文本")
    toc = [SourceToc(title, index) for index, (title, _) in enumerate(chapters)]
    return BookSource(path=path, title=path.stem, kind="txt", chapters=chapters, toc=toc)


def read_cbz_source(path: Path) -> BookSource:
    """CBZ = a ZIP of page images (ComicInfo.xml is ignored)."""
    path = Path(path)
    images: list[tuple[str, bytes, int, int]] = []
    with zipfile.ZipFile(path) as zf:
        names = [
            n for n in zf.namelist()
            if not n.endswith("/")
            and Path(n).suffix.lower() in IMAGE_EXTS
            and not Path(n).name.lower().startswith("comicinfo")
        ]
        for name in sort_images([Path(n) for n in names]):
            data = zf.read(str(name))
            try:
                width, height = _image_size_from_bytes(data)
            except Exception:
                width, height = 1200, 1800
            images.append((Path(str(name)).name, data, width, height))
    if not images:
        raise TranscodeError(f"{path.name} 里没有图片")
    return BookSource(path=path, title=path.stem, kind="cbz", images=images)


def _image_size_from_bytes(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return im.size


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_epub_source(path: Path) -> BookSource:
    """Read an EPUB: image pages become fixed-layout pages, text becomes chapters."""
    import xml.etree.ElementTree as ET

    path = Path(path)
    images: list[tuple[str, bytes, int, int]] = []
    chapters: list[tuple[str, list[str]]] = []
    titles: dict[str, str] = {}  # spine href -> title (for the TOC)

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        def read(rel: str) -> bytes:
            return zf.read(rel)

        rootfile = ""
        try:
            container = ET.fromstring(read("META-INF/container.xml"))
            for node in container.iter():
                if _local_name(node.tag) == "rootfile" and node.get("full-path"):
                    rootfile = node.get("full-path")
                    break
        except Exception as exc:
            raise TranscodeError(f"{path.name} 不是有效的 EPUB：{exc}") from exc
        if not rootfile or rootfile not in names:
            raise TranscodeError(f"{path.name} 缺少 OPF")

        opf_dir = str(Path(rootfile).parent).replace("\\", "/")
        if opf_dir == ".":
            opf_dir = ""
        opf = ET.fromstring(read(rootfile))

        manifest: dict[str, tuple[str, str, str]] = {}  # id -> (href, media, props)
        for node in opf.iter():
            if _local_name(node.tag) == "item":
                item_id = node.get("id") or ""
                manifest[item_id] = (
                    node.get("href") or "",
                    node.get("media-type") or "",
                    node.get("properties") or "",
                )

        spine_ids: list[str] = []
        for node in opf.iter():
            if _local_name(node.tag) == "itemref":
                spine_ids.append(node.get("idref") or "")

        def join(base: str, href: str) -> str:
            href = href.split("#", 1)[0]
            return f"{base}/{href}" if base else href

        def text_of(xhtml: bytes) -> list[str]:
            try:
                root = ET.fromstring(xhtml)
            except Exception:
                stripped = re.sub(rb"<[^>]+>", b"\n", xhtml).decode("utf-8", "replace")
                return [s.strip() for s in stripped.split("\n") if s.strip()]
            paras = []
            for node in root.iter():
                if _local_name(node.tag) in ("p", "div", "h1", "h2", "h3"):
                    chunk = "".join(node.itertext()).strip()
                    if chunk:
                        paras.append(chunk)
            return paras or ["".join(root.itertext()).strip()]

        # Remember where each spine document landed so the source's nav can be
        # remapped onto real page indices.
        href_to_page: dict[str, int] = {}
        for item_id in spine_ids:
            href, media, _props = manifest.get(item_id, ("", "", ""))
            if not href:
                continue
            rel = join(opf_dir, href)
            if rel not in names:
                continue
            raw = read(rel)
            # Collect <img>/<image> targets in this spine document.
            found = re.findall(
                rb'(?:src|xlink:href|href)\s*=\s*["\']([^"\']+\.(?:jpg|jpeg|png|gif|webp|bmp|avif|tiff?))["\']',
                raw,
                re.IGNORECASE,
            )
            first_page = len(images)
            added = False
            for target in found:
                img_rel = join(str(Path(rel).parent).replace("\\", "/"), target.decode("utf-8", "replace"))
                if img_rel not in names:
                    continue
                data = read(img_rel)
                try:
                    width, height = _image_size_from_bytes(data)
                except Exception:
                    width, height = 1200, 1800
                images.append((Path(img_rel).name, data, width, height))
                added = True
            if added:
                href_to_page[rel] = first_page
            else:
                paras = [p for p in text_of(raw) if p]
                if paras:
                    href_to_page[rel] = len(chapters)
                    chapters.append((Path(rel).stem, paras))

        # The EPUB's own navigation gives us the nested TOC (and page titles).
        nav_href = ""
        for href, _media, props in manifest.values():
            if "nav" in props.split():
                nav_href = href
                break
        if nav_href:
            nav_rel = join(opf_dir, nav_href)
            if nav_rel in names:
                try:
                    nav = ET.fromstring(read(nav_rel))
                except Exception:
                    nav = None
                if nav is not None:
                    _collect_nav_titles(nav, titles)

    toc: list[SourceToc] = []
    seen: set[str] = set()
    for href, title in titles.items():
        if title in seen:
            continue
        seen.add(title)
        page = href_to_page.get(join(opf_dir, href.split("#", 1)[0]))
        # Only image pages can be addressed by the merged book's nested list.
        if page is not None and page < len(images):
            toc.append(SourceToc(title, page))
    if not images and not chapters:
        raise TranscodeError(f"{path.name} 里没有可读内容")
    return BookSource(path=path, title=path.stem, kind="epub", images=images,
                      chapters=chapters, toc=toc)


def _collect_nav_titles(nav, titles: dict[str, str]) -> None:
    """Fill href -> title from a nav document (best effort)."""
    for node in nav.iter():
        if _local_name(node.tag) != "a":
            continue
        href = (node.get("href") or "").split("#", 1)[0]
        label = "".join(node.itertext()).strip()
        if href and label:
            titles.setdefault(href, label)


def read_pdf_source(path: Path) -> BookSource:
    """Extract embedded JPEG (DCTDecode) images from a PDF, in page order.

    Works for scanned/comic PDFs and for the PDFs this tool produces. Text-only
    PDFs cannot be rasterised without a renderer, so they are reported clearly.
    """
    path = Path(path)
    raw = path.read_bytes()
    objects: dict[int, bytes] = {}
    for match in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", raw, re.DOTALL):
        objects[int(match.group(1))] = match.group(2)

    def stream_of(body: bytes) -> bytes:
        start = body.find(b"stream")
        if start < 0:
            return b""
        start += len(b"stream")
        if body[start:start + 2] == b"\r\n":
            start += 2
        elif body[start:start + 1] in (b"\n", b"\r"):
            start += 1
        end = body.rfind(b"endstream")
        return body[start:end] if end >= 0 else body[start:]

    # Page order first; fall back to object order.
    order: list[int] = []
    for body in objects.values():
        if b"/Type" in body and re.search(rb"/Type\s*/Page\b", body):
            for ref in re.findall(rb"(\d+)\s+0\s+R", body):
                num = int(ref)
                if b"/Subtype" in objects.get(num, b"") and b"/Image" in objects.get(num, b""):
                    order.append(num)
    if not order:
        order = [
            num for num, body in sorted(objects.items())
            if b"/Subtype" in body and b"/Image" in body and b"/DCTDecode" in body
        ]

    images: list[tuple[str, bytes, int, int]] = []
    for num in order:
        body = objects.get(num, b"")
        if b"/DCTDecode" not in body:
            continue
        data = stream_of(body)
        if not data.startswith(b"\xff\xd8"):
            continue
        try:
            width, height, _ = _jpeg_dimensions(data)
        except Exception:
            try:
                width, height = _image_size_from_bytes(data)
            except Exception:
                width, height = 1200, 1800
        images.append((f"{len(images) + 1:04d}.jpg", data, width, height))

    if not images:
        raise TranscodeError(
            f"{path.name} 里没有可提取的图片（纯文字 PDF 需要渲染器，暂不支持合并）"
        )
    return BookSource(path=path, title=path.stem, kind="pdf", images=images)


def read_book_source(path: Path) -> BookSource:
    """Dispatch on the file extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if not path.is_file():
        raise TranscodeError(f"文件不存在：{path}")
    if suffix == ".epub":
        return read_epub_source(path)
    if suffix == ".cbz":
        return read_cbz_source(path)
    if suffix == ".pdf":
        return read_pdf_source(path)
    if suffix in (".txt", ".md"):
        return read_txt_source(path)
    raise TranscodeError(f"不支持的文件类型：{suffix or path.name}")


# ------------------------------------------------------------------- merging


def merge_sources(
    sources: Sequence[BookSource],
    target: Path,
    *,
    fmt: str = "epub",
    title: str = "",
    author: str = "",
    keep_source_toc: bool = True,
) -> Path:
    """Merge several source books into one ebook that carries a nested TOC."""
    sources = [s for s in sources if s.images or s.chapters]
    if not sources:
        raise TranscodeError("没有可合并的内容")
    book_title = title or target.stem
    has_text = any(s.chapters for s in sources)
    has_images = any(s.images for s in sources)

    if fmt in ("pdf", "cbz") and has_text:
        raise TranscodeError("PDF / CBZ 无法容纳文字章节，含 TXT 的合集请选择 EPUB")

    if fmt == "epub":
        docs: list[EpubDoc] = []
        toc: list[EpubTocEntry] = []
        for src_no, src in enumerate(sources, 1):
            first_doc = len(docs) + 1
            children: list[EpubTocEntry] = []
            # Namespace each source's assets: two books both containing
            # "0001.jpg" must not collide inside one package.
            prefix = f"images/s{src_no:03d}"
            if src.images:
                for name, data, width, height in src.images:
                    href = f"{prefix}/{name}"
                    docs.append(
                        EpubDoc(
                            title=f"{src.title} - {name}",
                            body=_EPUB_PAGE.format(
                                title=escape(name), w=width, h=height, src=href
                            ),
                            assets=[(href, data, _image_media_type(name))],
                            # The collection's first image is its cover.
                            cover=not any(d.cover for d in docs),
                        )
                    )
            if src.chapters:
                for chapter_title, paras in src.chapters:
                    body = "".join(f"<p>{html_escape(p)}</p>" for p in paras)
                    docs.append(
                        EpubDoc(
                            title=chapter_title,
                            body=_EPUB_TEXT_PAGE.format(
                                title=html_escape(chapter_title),
                                css=_EPUB_TEXT_CSS,
                                body=f"<h2>{html_escape(chapter_title)}</h2>{body}",
                            ),
                            layout="reflowable",
                        )
                    )
            # The source's own TOC is preserved as a nested list, pointing at
            # the page each entry actually referred to.
            if keep_source_toc:
                if src.chapters:
                    # A heading-less book is one chapter named after the book;
                    # the book's own TOC entry already says that, so do not
                    # repeat it as a child.
                    if len(src.chapters) == 1 and src.chapters[0][0] == src.title:
                        children = []
                    else:
                        children = [
                            EpubTocEntry(ct, f"page{first_doc + i:04d}.xhtml")
                            for i, (ct, _p) in enumerate(src.chapters)
                        ]
                elif src.toc:
                    count = len(src.images)
                    children = [
                        EpubTocEntry(
                            e.title,
                            f"page{first_doc + min(max(e.page_index, 0), count - 1):04d}.xhtml",
                        )
                        for e in src.toc
                    ]
            toc.append(EpubTocEntry(src.title, f"page{first_doc:04d}.xhtml", children))
        return write_epub(target, title=book_title, author=author, docs=docs, toc=toc)

    # PDF / CBZ: images only.
    flat: list[tuple[str, bytes, int, int]] = []
    outline: list[SourceToc] = []
    for src in sources:
        start = len(flat)
        flat.extend(src.images)
        children = [
            SourceToc(child.title, start + child.page_index, list(child.children))
            for child in src.toc
        ] if keep_source_toc else []
        outline.append(SourceToc(src.title, start, children))
    if fmt == "cbz":
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
            for index, (name, data, _w, _h) in enumerate(flat, 1):
                zf.writestr(f"{index:04d}{Path(name).suffix.lower()}", data)
            zf.writestr("ComicInfo.xml", _comic_info_xml(book_title, len(flat), author))
        return target
    if fmt == "pdf":
        return build_pdf_from_images(
            flat, target, title=book_title, author=author, outline=outline
        )
    raise TranscodeError(f"不支持的输出格式：{fmt}")


# ---------------------------------------------------------------------- engine


# Placeholders accepted by the output-name template. ``title``/``folder`` fall
# back to each other so a half-filled form still yields sensible names.
NAME_PLACEHOLDERS = ("title", "folder", "index", "index2", "index3", "total", "date")


def render_book_name(
    template: str,
    *,
    title: str = "",
    folder: str = "",
    index: int = 1,
    total: int = 1,
) -> str:
    """Turn a name template into one output name.

    Empty template falls back to sensible defaults: a single book uses the
    user's title (or the folder name); several books are disambiguated by the
    folder name so they do not all collapse onto one filename.
    """
    import datetime as _datetime

    title = (title or "").strip()
    folder = (folder or "").strip()
    values = {
        "title": title or folder or "ebook",
        "folder": folder or title or "ebook",
        "index": str(index),
        "index2": f"{index:02d}",
        "index3": f"{index:03d}",
        "total": str(total),
        "date": _datetime.date.today().strftime("%Y%m%d"),
    }
    if not template.strip():
        if total > 1:
            return f"{title} - {folder}" if title and folder else (folder or title or "ebook")
        return title or folder or "ebook"
    try:
        rendered = template.format(**values)
    except KeyError as exc:
        raise TranscodeError(
            f"命名模板里的 {{{exc.args[0]}}} 不是可用占位符（可用：{', '.join(NAME_PLACEHOLDERS)}）"
        ) from exc
    except (IndexError, ValueError) as exc:
        raise TranscodeError(f"命名模板有误：{template}（{exc}）") from exc
    return rendered.strip() or values["title"]


def plan_names(
    groups: Sequence[dict[str, Any]],
    *,
    fmt: str = "epub",
    title: str = "",
    name_template: str = "",
    target_dir: Optional[Path] = None,
    overwrite: bool = False,
) -> list[dict[str, str]]:
    """Predict the exact output filenames for the current settings.

    The UI calls this to show a live preview, and :meth:`EbookBuilder.build`
    uses it for the real names — so what the user sees is what gets written.
    """
    total = len(groups)
    taken: set[str] = set()
    planned: list[dict[str, str]] = []
    for index, group in enumerate(groups, 1):
        folder = str(group.get("name") or "").strip()
        book = render_book_name(
            name_template, title=title, folder=folder, index=index, total=total
        )
        stem = safe_filename(book)
        name = f"{stem}.{fmt}"
        if target_dir is not None:
            directory = Path(target_dir)
            counter = 1
            while (not overwrite and (directory / name).exists()) or name in taken:
                name = f"{stem}-{counter}.{fmt}"
                counter += 1
        taken.add(name)
        planned.append({"group": folder, "title": book, "file": name})
    return planned


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
                    im.save(buf, format="JPEG", quality=82, optimize=True)
                entry["data"] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as exc:  # keep the row even if the thumb fails
                entry["error"] = str(exc)
            out.append(entry)
        return out

    def preview_data_uri(
        self, path: Path, *, max_size: int = 1800, quality: int = 88
    ) -> dict[str, Any]:
        """A large, single-image preview (for the double-click lightbox).

        Capped at ``max_size`` on the long edge so a full-resolution comic page
        (often 2-6 MB) still transfers quickly while remaining readable.
        """
        from PIL import Image, ImageOps

        path = Path(path)
        if not path.is_file():
            raise TranscodeError(f"图片不存在：{path}")
        with Image.open(path) as im:
            original = im.size
            im = ImageOps.exif_transpose(im) or im
            im.thumbnail((max_size, max_size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            shown = im.size
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
        return {
            "path": str(path),
            "name": path.name,
            "data": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
            "width": original[0],
            "height": original[1],
            "shownWidth": shown[0],
            "shownHeight": shown[1],
        }

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
        name_template: str = "",
    ) -> list[Path]:
        """Build one ebook per group; returns the written paths.

        ``groups`` is the (possibly user-reordered) ``[{name, images:[...]}]``
        structure: ``images`` entries may be dicts with ``path`` or paths.
        Names come from :func:`plan_names`, so the UI preview is exact.
        """
        target_dir = ensure_directory(Path(target_dir).expanduser().resolve())
        fmt = (fmt or "cbz").lower()
        if fmt not in ("pdf", "cbz", "epub"):
            raise TranscodeError(f"不支持的电子书格式：{fmt}")

        # Skip empty groups up front so naming/indices match the preview.
        groups = [g for g in groups if g.get("images")]
        if not groups:
            raise TranscodeError("没有可转换的图片")

        planned = plan_names(
            groups,
            fmt=fmt,
            title=title,
            name_template=name_template,
            target_dir=target_dir,
            overwrite=overwrite,
        )

        results: list[Path] = []
        total_groups = max(1, len(groups))
        for index, group in enumerate(groups, 1):
            if self._cancel:
                raise CanceledError("ebook build cancelled by user")
            pages = [_coerce_image_path(img) for img in group.get("images", [])]
            missing = [p for p in pages if not p.is_file()]
            if missing:
                raise TranscodeError(f"图片不存在：{missing[0]}")
            book_title = planned[index - 1]["title"]
            target = target_dir / planned[index - 1]["file"]

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

    # ------------------------------------------------------------------ merge

    async def merge(
        self,
        files: Sequence[Path | str],
        target_dir: Path,
        *,
        fmt: str = "epub",
        title: str = "",
        author: str = "",
        overwrite: bool = False,
        keep_source_toc: bool = True,
    ) -> list[Path]:
        """Merge several ebooks / TXT files into one book that carries a TOC."""
        import os

        target_dir = ensure_directory(Path(target_dir).expanduser().resolve())
        fmt = (fmt or "epub").lower()
        paths = [Path(f) for f in files if str(f).strip()]
        if not paths:
            raise TranscodeError("没有选择要合并的文件")

        sources: list[BookSource] = []
        total = len(paths)
        for index, path in enumerate(paths, 1):
            if self._cancel:
                raise CanceledError("ebook merge cancelled by user")
            self._emit(
                "running",
                progress=(index - 1) / (total + 1),
                percent=(index - 1) / (total + 1) * 100,
                detail=f"正在读取 {path.name}（{index}/{total}）",
            )
            sources.append(await asyncio.to_thread(read_book_source, path))

        book_title = title.strip() or "合集"
        stem = safe_filename(render_book_name(title or "合集", title=title or "合集"))
        target = target_dir / f"{stem}.{fmt}"
        if not overwrite and target.exists():
            target = unique_path(target)

        self._emit(
            "running",
            progress=total / (total + 1),
            percent=total / (total + 1) * 100,
            detail=f"正在合并 {len(sources)} 本 → {target.name}",
        )
        pages = sum(len(s.images) for s in sources)
        chapters = sum(len(s.chapters) for s in sources)
        temp = target.with_name(f".{target.name}.part-{os.getpid()}")
        try:
            await asyncio.to_thread(
                merge_sources,
                sources,
                temp,
                fmt=fmt,
                title=book_title,
                author=author,
                keep_source_toc=keep_source_toc,
            )
            os.replace(temp, target)
        except Exception:
            temp.unlink(missing_ok=True)
            raise

        self._emit(
            "finished",
            progress=1.0,
            percent=100.0,
            detail=f"已合并 {len(sources)} 本（{pages} 页 / {chapters} 章）→ {target.name}",
        )
        return [target]
