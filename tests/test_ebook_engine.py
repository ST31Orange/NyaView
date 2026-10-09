"""Tests for the image-pack -> ebook engine (PDF / CBZ / EPUB)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from nyavview.core.errors import TranscodeError
from nyavview.core.settings import Settings
from nyavview.engines.ebook import (
    EbookBuilder,
    build_cbz,
    build_epub,
    build_pdf,
    natural_key,
    scan_images,
    sort_images,
    subfolder_groups,
)


# ------------------------------------------------------------------ fixtures


def _make_image(path: Path, size=(400, 600), colour=(200, 180, 160)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


@pytest.fixture()
def pack(tmp_path: Path) -> Path:
    """A pack whose names only sort correctly with a natural sort."""
    folder = tmp_path / "pack"
    for name in ["10.jpg", "2.jpg", "1.jpg", "20.jpg", "3.jpg"]:
        _make_image(folder / name)
    return folder


def _engine() -> EbookBuilder:
    return EbookBuilder(Settings())


# ------------------------------------------------------------------- sorting


def test_natural_key_orders_numbers_numerically() -> None:
    assert natural_key("2.jpg") < natural_key("10.jpg")
    assert natural_key("1.jpg") < natural_key("2.jpg") < natural_key("10.jpg")


def test_natural_sort_handles_mixed_cjk_and_numbers(tmp_path: Path) -> None:
    """Numeric and text chunks must never compare directly (regression: mixing
    ``1.png`` with a folder named ``第2话`` raised TypeError)."""
    root = tmp_path / "root"
    _make_image(root / "1.png")
    _make_image(root / "10.png")
    _make_image(root / "第2话" / "1.png")
    _make_image(root / "第2话" / "2.png")
    ordered = [str(p.relative_to(root)) for p in scan_images(root)]
    assert "1.png" in ordered and "10.png" in ordered
    assert ordered.index("1.png") < ordered.index("10.png")
    # deep groups sort deterministically too
    groups = subfolder_groups(root)
    assert [name for name, _ in groups] == ["root", "第2话"]


def test_sort_images_natural(tmp_path: Path) -> None:
    folder = tmp_path / "p"
    names = ["10.jpg", "2.jpg", "1.jpg", "20.jpg", "3.jpg"]
    for n in names:
        _make_image(folder / n)
    ordered = [p.name for p in sort_images([folder / n for n in names], folder)]
    assert ordered == ["1.jpg", "2.jpg", "3.jpg", "10.jpg", "20.jpg"]


def test_scan_images_orders_and_filters(pack: Path) -> None:
    (pack / "notes.txt").write_text("x", encoding="utf-8")
    (pack / "sub").mkdir()
    ordered = [p.name for p in scan_images(pack)]
    assert ordered == ["1.jpg", "2.jpg", "3.jpg", "10.jpg", "20.jpg"]


def test_scan_images_raises_when_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TranscodeError):
        scan_images(empty)


def test_subfolder_groups(tmp_path: Path) -> None:
    root = tmp_path / "comic"
    _make_image(root / "cover.jpg")
    _make_image(root / "ch1" / "1.jpg")
    _make_image(root / "ch1" / "2.jpg")
    _make_image(root / "ch2" / "1.jpg")
    groups = subfolder_groups(root)
    assert [name for name, _ in groups] == ["comic", "ch1", "ch2"]
    assert [len(imgs) for _, imgs in groups] == [1, 2, 1]


# ----------------------------------------------------------------------- CBZ


def test_build_cbz_structure_and_order(pack: Path, tmp_path: Path) -> None:
    pages = scan_images(pack)
    out = build_cbz(pages, tmp_path / "book.cbz", title="测试漫画")
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    # numbered, zero-padded, in natural order
    assert names[:5] == ["0001.jpg", "0002.jpg", "0003.jpg", "0004.jpg", "0005.jpg"]
    assert "ComicInfo.xml" in names
    with zipfile.ZipFile(out) as zf:
        info = zf.read("ComicInfo.xml").decode("utf-8")
    assert "<PageCount>5</PageCount>" in info
    assert "测试漫画" in info


def test_build_cbz_can_skip_comic_info(pack: Path, tmp_path: Path) -> None:
    out = build_cbz(scan_images(pack), tmp_path / "b.cbz", comic_info=False)
    with zipfile.ZipFile(out) as zf:
        assert "ComicInfo.xml" not in zf.namelist()


# ----------------------------------------------------------------------- PDF


def test_build_pdf_is_valid_and_has_all_pages(pack: Path, tmp_path: Path) -> None:
    pages = scan_images(pack)
    out = build_pdf(pages, tmp_path / "book.pdf", title="测试")
    data = out.read_bytes()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")
    assert data.count(b"/Type /Page ") == 5  # one per page (not /Pages)
    assert data.count(b"/Subtype /Image") == 5
    assert b"%%EOF" in data


def test_build_pdf_embeds_jpeg_losslessly(tmp_path: Path) -> None:
    """Upright JPEG pages must be embedded byte-for-byte (no re-encode)."""
    from PIL import Image

    src = tmp_path / "p.jpg"
    Image.new("RGB", (300, 450), (10, 120, 200)).save(src, quality=92)
    raw = src.read_bytes()
    out = build_pdf([src], tmp_path / "o.pdf")
    assert raw in out.read_bytes()


def test_build_pdf_accepts_png(tmp_path: Path) -> None:
    from PIL import Image

    src = tmp_path / "p.png"
    Image.new("RGB", (120, 160), (30, 30, 30)).save(src)
    out = build_pdf([src], tmp_path / "o.pdf")
    assert out.stat().st_size > 0
    assert out.read_bytes().count(b"/Subtype /Image") == 1


# ---------------------------------------------------------------------- EPUB


def test_build_epub_structure(pack: Path, tmp_path: Path) -> None:
    out = build_epub(scan_images(pack), tmp_path / "book.epub", title="漫画名", author="作者")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
        # mimetype must be first and stored uncompressed
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
    assert "META-INF/container.xml" in names
    assert sum(1 for n in names if n.startswith("OEBPS/page")) == 5
    assert sum(1 for n in names if n.startswith("OEBPS/images/")) == 5
    # fixed layout + metadata
    assert 'name="rendition:layout">pre-paginated' in opf
    assert "<dc:title>漫画名</dc:title>" in opf
    assert "<dc:creator>作者</dc:creator>" in opf
    assert opf.count("<itemref") == 5


def test_build_epub_pages_reference_images(pack: Path, tmp_path: Path) -> None:
    out = build_epub(scan_images(pack), tmp_path / "b.epub")
    with zipfile.ZipFile(out) as zf:
        page = zf.read("OEBPS/page0001.xhtml").decode("utf-8")
    assert 'src="images/0001.jpg"' in page


# -------------------------------------------------------------------- builder


def test_builder_scan_merge(pack: Path) -> None:
    info = _engine().scan(pack)
    assert info["total"] == 5
    assert len(info["groups"]) == 1
    assert [Path(i["path"]).name for i in info["images"]] == [
        "1.jpg",
        "2.jpg",
        "3.jpg",
        "10.jpg",
        "20.jpg",
    ]


def test_builder_scan_per_folder(tmp_path: Path) -> None:
    root = tmp_path / "c"
    _make_image(root / "ch1" / "1.jpg")
    _make_image(root / "ch2" / "1.jpg")
    _make_image(root / "ch2" / "2.jpg")
    info = _engine().scan(root, mode="per_folder")
    assert info["total"] == 3
    assert [g["name"] for g in info["groups"]] == ["ch1", "ch2"]


@pytest.mark.asyncio
async def test_builder_build_merge_writes_one_file(pack: Path, tmp_path: Path) -> None:
    builder = _engine()
    info = builder.scan(pack)
    out = await builder.build(info["groups"], tmp_path / "out", fmt="cbz", title="合集")
    assert len(out) == 1
    assert out[0].name == "合集.cbz"
    assert out[0].exists()


@pytest.mark.asyncio
async def test_builder_build_per_folder_writes_one_per_group(tmp_path: Path) -> None:
    root = tmp_path / "c"
    _make_image(root / "ch1" / "1.jpg")
    _make_image(root / "ch2" / "1.jpg")
    builder = _engine()
    info = builder.scan(root, mode="per_folder")
    out = await builder.build(info["groups"], tmp_path / "out", fmt="cbz")
    assert sorted(p.stem for p in out) == ["ch1", "ch2"]


@pytest.mark.asyncio
async def test_builder_per_folder_names_each_book_distinctly(tmp_path: Path) -> None:
    """A shared title must not collapse every per-folder book onto one name."""
    root = tmp_path / "c"
    _make_image(root / "ch1" / "1.jpg")
    _make_image(root / "ch2" / "1.jpg")
    builder = _engine()
    info = builder.scan(root, mode="per_folder")
    out = await builder.build(info["groups"], tmp_path / "out", fmt="cbz", title="合集")
    assert sorted(p.stem for p in out) == ["合集 - ch1", "合集 - ch2"]
    assert len({p.name for p in out}) == 2


@pytest.mark.asyncio
async def test_builder_respects_manual_order(pack: Path, tmp_path: Path) -> None:
    """The UI sends an explicit order; it must win over the natural sort."""
    builder = _engine()
    info = builder.scan(pack)
    images = list(reversed(info["images"]))
    out = await builder.build([{"name": "reversed", "images": images}], tmp_path / "o", fmt="cbz")
    with zipfile.ZipFile(out[0]) as zf:
        first = zf.namelist()[0]
        expected = Path(images[0]["path"]).suffix.lower()
    assert first.endswith(expected)
    assert first == "0001.jpg"


@pytest.mark.asyncio
async def test_builder_rejects_unknown_format(pack: Path, tmp_path: Path) -> None:
    builder = _engine()
    info = builder.scan(pack)
    with pytest.raises(TranscodeError):
        await builder.build(info["groups"], tmp_path / "o", fmt="docx")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_builder_missing_image_raises(pack: Path, tmp_path: Path) -> None:
    builder = _engine()
    with pytest.raises(TranscodeError):
        await builder.build(
            [{"name": "x", "images": [{"path": str(tmp_path / "nope.jpg")}]}],
            tmp_path / "o",
            fmt="cbz",
        )


def test_builder_thumbnails_are_data_uris(pack: Path) -> None:
    thumbs = _engine().thumbnails(scan_images(pack)[:2], size=80)
    assert len(thumbs) == 2
    for t in thumbs:
        assert t["data"].startswith("data:image/jpeg;base64,")


def test_builder_thumbnail_survives_broken_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    thumbs = _engine().thumbnails([bad])
    assert "error" in thumbs[0]


def test_preview_data_uri_downscales_and_reports_original(tmp_path: Path) -> None:
    """The lightbox image is capped but still reports the original size."""
    src = tmp_path / "big.jpg"
    _make_image(src, size=(2400, 3600))
    info = _engine().preview_data_uri(src, max_size=1200)
    assert info["data"].startswith("data:image/jpeg;base64,")
    assert (info["width"], info["height"]) == (2400, 3600)
    assert max(info["shownWidth"], info["shownHeight"]) <= 1200
    # A small image is not upscaled.
    small = tmp_path / "small.jpg"
    _make_image(small, size=(320, 480))
    got = _engine().preview_data_uri(small, max_size=1200)
    assert (got["shownWidth"], got["shownHeight"]) == (320, 480)


def test_preview_data_uri_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TranscodeError):
        _engine().preview_data_uri(tmp_path / "nope.jpg")


@pytest.mark.asyncio
async def test_builder_tolerates_wrapped_or_dict_page_entries(pack: Path, tmp_path: Path) -> None:
    """A single-element list or ``{"path": ...}`` entry must still work (the
    bridge can hand us either shape)."""
    builder = _engine()
    info = builder.scan(pack)
    images = [
        [info["images"][0]["path"]],  # accidental single-element list
        info["images"][1],  # {"path": ...} dict from scan()
        info["images"][2]["path"],  # plain string
    ]
    out = await builder.build([{"name": "mixed", "images": images}], tmp_path / "o", fmt="cbz")
    assert out[0].exists()
    with zipfile.ZipFile(out[0]) as zf:
        assert len([n for n in zf.namelist() if n.endswith(".jpg")]) == 3
