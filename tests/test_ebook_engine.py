"""Tests for the image-pack -> ebook engine (PDF / CBZ / EPUB)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from nyavview.core.errors import TranscodeError
from nyavview.core.settings import Settings
from nyavview.engines.ebook import (
    BookSource,
    EbookBuilder,
    build_cbz,
    build_epub,
    build_pdf,
    is_chapter_heading,
    merge_sources,
    natural_key,
    plan_names,
    read_book_source,
    read_text_file,
    render_book_name,
    scan_images,
    sort_images,
    split_text_chapters,
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
    # EPUB3 rendition properties MUST use property= (regression: name= made
    # readers treat the book as reflowable and paginate each page, which showed
    # up as blank pages when turning pages).
    assert '<meta property="rendition:layout">pre-paginated</meta>' in opf
    assert 'name="rendition:layout"' not in opf
    assert '<meta property="rendition:spread">none</meta>' in opf
    assert "<dc:title>漫画名</dc:title>" in opf
    assert "<dc:creator>作者</dc:creator>" in opf
    assert opf.count("<itemref") == 5
    # every spine item is explicitly pre-paginated
    assert opf.count('properties="rendition:layout-pre-paginated"') == 5
    # the cover image is flagged so readers show a real cover
    assert 'properties="cover-image"' in opf
    # nav must not be a reading page
    assert "nav.xhtml" not in opf.split("<spine>")[1]


def test_build_epub_pages_fit_one_viewport(pack: Path, tmp_path: Path) -> None:
    """Each page must declare its own size and clip overflow, otherwise a
    paginating reader splits it and emits a blank page."""
    out = build_epub(scan_images(pack), tmp_path / "b.epub")
    with zipfile.ZipFile(out) as zf:
        page = zf.read("OEBPS/page0001.xhtml").decode("utf-8")
    assert 'xlink:href="images/0001.jpg"' in page
    assert 'viewBox="0 0 400 600"' in page
    assert 'preserveAspectRatio="xMidYMid meet"' in page
    assert '<meta name="viewport" content="width=400, height=600"/>' in page
    assert "overflow:hidden" in page


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


# ------------------------------------------------------- naming (item: 书名)

def test_render_book_name_defaults() -> None:
    # one book: the user's title wins, else the folder name
    assert render_book_name("", title="亚人", folder="亚人漫画", index=1, total=1) == "亚人"
    assert render_book_name("", title="", folder="亚人漫画", index=1, total=1) == "亚人漫画"
    # several books must not collapse onto one name
    assert render_book_name("", title="", folder="第1卷", index=1, total=3) == "第1卷"
    assert render_book_name("", title="合集", folder="第1卷", index=1, total=3) == "合集 - 第1卷"


def test_render_book_name_template_placeholders() -> None:
    got = render_book_name("{title}·{folder}({index2}/{total})", title="亚人", folder="第3卷", index=3, total=9)
    assert got == "亚人·第3卷(03/9)"


def test_render_book_name_rejects_unknown_placeholder() -> None:
    with pytest.raises(TranscodeError):
        render_book_name("{nope}", title="x", folder="y")


def test_plan_names_matches_what_build_writes(tmp_path: Path) -> None:
    """The preview must be exactly what gets produced."""
    groups = [{"name": "第1卷", "images": ["a"]}, {"name": "第2卷", "images": ["b"]}]
    planned = plan_names(groups, fmt="epub", title="亚人", target_dir=tmp_path)
    assert [p["file"] for p in planned] == ["亚人 - 第1卷.epub", "亚人 - 第2卷.epub"]


def test_plan_names_dedupes_existing_and_duplicate_targets(tmp_path: Path) -> None:
    (tmp_path / "合集.epub").write_bytes(b"x")
    groups = [{"name": "", "images": ["a"]}]
    assert plan_names(groups, fmt="epub", title="合集", target_dir=tmp_path)[0]["file"] == "合集-1.epub"
    # two groups rendering the same name are separated even when overwriting
    same = [{"name": "同一话", "images": ["a"]}, {"name": "同一话", "images": ["b"]}]
    got = plan_names(same, fmt="cbz", title="", target_dir=tmp_path, overwrite=True)
    assert len({p["file"] for p in got}) == 2


@pytest.mark.asyncio
async def test_build_uses_plan_names(tmp_path: Path) -> None:
    pack_a, pack_b = tmp_path / "a", tmp_path / "b"
    _make_image(pack_a / "1.jpg")
    _make_image(pack_b / "1.jpg")
    builder = _engine()
    groups = [{"name": "第1卷", "images": [str(pack_a / "1.jpg")]},
              {"name": "第2卷", "images": [str(pack_b / "1.jpg")]}]
    out = await builder.build(groups, tmp_path / "out", fmt="cbz", title="亚人")
    assert sorted(p.name for p in out) == ["亚人 - 第1卷.cbz", "亚人 - 第2卷.cbz"]


# ------------------------------------------------------------------ TXT

def test_read_text_file_handles_gb18030(tmp_path: Path) -> None:
    """Chinese novels are commonly GBK/GB18030, not UTF-8."""
    path = tmp_path / "gbk.txt"
    path.write_bytes("第一章 开始\n正文内容。\n".encode("gb18030"))
    assert "第一章 开始" in read_text_file(path)


def test_read_text_file_handles_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.txt"
    path.write_bytes("\ufeff第一章 开始\n".encode("utf-8"))
    text = read_text_file(path)
    assert text.startswith("第一章")


def test_is_chapter_heading_recognises_common_forms() -> None:
    for line in ["第一章", "第1章 开端", "第 12 节", "第三卷", "Chapter 4", "序章", "番外 1"]:
        assert is_chapter_heading(line), line
    for line in ["这是一段很长的正文，讲了第一章里发生的很多事情，不该被当成标题处理。", "", "他说：第1章很好看"]:
        assert not is_chapter_heading(line), line


def test_split_text_chapters_splits_on_headings() -> None:
    text = "第一章 起\n甲\n乙\n第二章 承\n丙\n"
    chapters = split_text_chapters(text)
    assert [c[0] for c in chapters] == ["第一章 起", "第二章 承"]
    assert chapters[0][1] == ["甲", "乙"]


def test_split_text_chapters_untitled_book_uses_the_book_name() -> None:
    """A file with no headings is ONE chapter named after the book — not a run
    of generic "第 N 部分" entries (which made merged TOCs a wall of "第 1 部分")."""
    text = "第一段正文。\n第二段正文。\n第三段正文。"
    chapters = split_text_chapters(text, book_title="大衮")
    assert [c[0] for c in chapters] == ["大衮"]
    assert chapters[0][1] == ["第一段正文。", "第二段正文。", "第三段正文。"]
    # without a book title it falls back to a neutral label, never 第 N 部分
    assert split_text_chapters(text)[0][0] == "正文"


def test_split_text_chapters_single_heading_is_kept() -> None:
    """Regression: a book with exactly one heading used to have that heading
    thrown away and replaced by numbered parts."""
    text = "第一章 起\n甲甲甲\n乙乙乙\n"
    assert [c[0] for c in split_text_chapters(text, book_title="某书")] == ["第一章 起"]


def test_split_text_chapters_huge_untitled_book_splits_with_book_name() -> None:
    text = "\n".join("正" * 200 for _ in range(2000))  # ~400k chars, no headings
    chapters = split_text_chapters(text, book_title="长夜")
    assert len(chapters) > 1
    assert all(c[0].startswith("长夜（") for c in chapters)
    assert chapters[0][0] == f"长夜（1/{len(chapters)}）"


def test_is_chapter_heading_recognises_roman_numerals() -> None:
    assert is_chapter_heading("I.粘土中的恐怖")
    assert is_chapter_heading("II. 风暴中的过客")
    assert is_chapter_heading("一、初见")
    # prose must not be mistaken for a heading
    assert not is_chapter_heading("I. 这是一个很长的英文句子，不应该被当作小标题来处理的情况。")
    assert not is_chapter_heading("我们第一章说的是另一个故事，很长很长的一段正文内容。")


@pytest.mark.asyncio
async def test_build_txt_into_epub_with_toc(tmp_path: Path) -> None:
    src = tmp_path / "小说.txt"
    src.write_text("第一章 起\n甲\n第二章 承\n乙\n", encoding="utf-8")
    out = await _engine().merge([src], tmp_path / "out", fmt="epub", title="小说合集")
    assert out[0].name == "小说合集.epub"
    with zipfile.ZipFile(out[0]) as zf:
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    # text is reflowable, so the package must not claim fixed layout
    assert '<meta property="rendition:layout">reflowable</meta>' in opf
    assert "第一章 起" in nav and "第二章 承" in nav
    assert nav.count("<ol>") >= 2  # nested: book -> chapters


def test_merge_rejects_pdf_output_for_text_sources(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_text("第一章\n正文\n", encoding="utf-8")
    source = read_book_source(src)
    with pytest.raises(TranscodeError):
        merge_sources([source], tmp_path / "x.pdf", fmt="pdf")


# ------------------------------------------------- merging source books

def _comic_book(tmp_path: Path, name: str, pages: int = 3) -> Path:
    folder = tmp_path / f"src-{name}"
    for i in range(1, pages + 1):
        _make_image(folder / f"{i}.jpg", colour=(100 + i * 10, 120, 150))
    return build_epub(scan_images(folder), tmp_path / f"{name}.epub", title=name)


def test_read_epub_source_roundtrip(tmp_path: Path) -> None:
    book = _comic_book(tmp_path, "甲", pages=3)
    source = read_book_source(book)
    assert source.kind == "epub"
    assert len(source.images) == 3
    assert source.images[0][1].startswith(b"\xff\xd8")
    assert source.images[0][2:4] == (400, 600)


def test_read_cbz_source_orders_pages_naturally(tmp_path: Path) -> None:
    folder = tmp_path / "cbz"
    for n in ["10.jpg", "2.jpg", "1.jpg"]:
        _make_image(folder / n)
    book = build_cbz(scan_images(folder), tmp_path / "b.cbz", title="乙")
    source = read_book_source(book)
    assert source.kind == "cbz"
    assert len(source.images) == 3
    # natural sort: 1, 2, 10
    assert [n for n, *_ in source.images] == ["0001.jpg", "0002.jpg", "0003.jpg"]


def test_read_pdf_source_extracts_jpegs(tmp_path: Path) -> None:
    pages = [_make_image(tmp_path / "p" / f"{i}.jpg") for i in (1, 2, 3)]
    book = build_pdf(pages, tmp_path / "p.pdf", title="丙")
    source = read_book_source(book)
    assert source.kind == "pdf"
    assert len(source.images) == 3
    assert all(data.startswith(b"\xff\xd8") for _n, data, _w, _h in source.images)


def test_read_book_source_rejects_unknown_type(tmp_path: Path) -> None:
    bad = tmp_path / "x.mobi"
    bad.write_bytes(b"x")
    with pytest.raises(TranscodeError):
        read_book_source(bad)


@pytest.mark.asyncio
async def test_merge_two_books_into_epub_with_nested_toc(tmp_path: Path) -> None:
    first = _comic_book(tmp_path, "第一卷", pages=2)
    second = _comic_book(tmp_path, "第二卷", pages=2)
    out = await _engine().merge([first, second], tmp_path / "out", fmt="epub", title="合集")
    assert out[0].name == "合集.epub"
    with zipfile.ZipFile(out[0]) as zf:
        names = zf.namelist()
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
    # every source page survives
    assert sum(1 for n in names if n.startswith("OEBPS/images/")) == 4
    assert opf.count("<itemref") == 4
    # both book titles appear as top-level TOC entries
    assert "第一卷" in nav and "第二卷" in nav
    # and each source's own TOC is nested underneath
    assert nav.count("<ol>") >= 2


def test_merge_to_pdf_adds_bookmark_outline(tmp_path: Path) -> None:
    a = _make_image(tmp_path / "a" / "1.jpg")
    b = _make_image(tmp_path / "b" / "1.jpg")
    src_a = BookSource(path=a, title="上册", kind="cbz", images=[("1.jpg", a.read_bytes(), 400, 600)])
    src_b = BookSource(path=b, title="下册", kind="cbz", images=[("1.jpg", b.read_bytes(), 400, 600)])
    out = merge_sources([src_a, src_b], tmp_path / "m.pdf", fmt="pdf", title="合集")
    raw = out.read_bytes()
    assert raw.startswith(b"%PDF")
    assert b"/Outlines" in raw
    assert b"/Title (\xe4\xb8\x8a\xe5\x86\x8c)" in raw  # 上册
    assert raw.rstrip().endswith(b"%%EOF")
    # two pages, two top-level bookmarks
    assert raw.count(b"/Type /Page ") == 2


def test_merge_to_cbz(tmp_path: Path) -> None:
    a = _make_image(tmp_path / "a" / "1.jpg")
    b = _make_image(tmp_path / "b" / "1.jpg")
    srcs = [
        BookSource(path=a, title="A", kind="cbz", images=[("1.jpg", a.read_bytes(), 400, 600)]),
        BookSource(path=b, title="B", kind="cbz", images=[("1.jpg", b.read_bytes(), 400, 600)]),
    ]
    out = merge_sources(srcs, tmp_path / "m.cbz", fmt="cbz", title="合集")
    with zipfile.ZipFile(out) as zf:
        entries = [n for n in zf.namelist() if n.endswith(".jpg")]
    assert entries == ["0001.jpg", "0002.jpg"]


def test_merge_preserves_source_toc_when_disabled(tmp_path: Path) -> None:
    a = _make_image(tmp_path / "a" / "1.jpg")
    src = BookSource(path=a, title="甲", kind="cbz", images=[("1.jpg", a.read_bytes(), 400, 600)])
    out = merge_sources([src], tmp_path / "x.epub", fmt="epub", title="单本", keep_source_toc=False)
    with zipfile.ZipFile(out) as zf:
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    assert nav.count("<ol>") == 1


@pytest.mark.asyncio
async def test_merge_mixed_txt_and_epub_collection(tmp_path: Path) -> None:
    """A collection can mix a novel (TXT) and a comic (EPUB)."""
    novel = tmp_path / "小说.txt"
    novel.write_text("第一章 起\n甲\n第二章 承\n乙\n", encoding="utf-8")
    comic = _comic_book(tmp_path, "漫画", pages=2)
    out = await _engine().merge([novel, comic], tmp_path / "out", fmt="epub", title="合集")
    with zipfile.ZipFile(out[0]) as zf:
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
    assert "小说" in nav and "漫画" in nav
    assert "第一章 起" in nav
    # mixed content: image pages keep their own pre-paginated override
    assert 'properties="rendition:layout-pre-paginated"' in opf


def test_merged_epub_has_no_duplicate_entries(tmp_path: Path) -> None:
    """Regression: two sources whose pages are both named 0001.jpg used to be
    written to the same package path, so images collided after merging."""
    a = _make_image(tmp_path / "a" / "0001.jpg")
    b = _make_image(tmp_path / "b" / "0001.jpg")
    srcs = [
        BookSource(path=a, title="甲", kind="cbz", images=[("0001.jpg", a.read_bytes(), 400, 600)]),
        BookSource(path=b, title="乙", kind="cbz", images=[("0001.jpg", b.read_bytes(), 400, 600)]),
    ]
    out = merge_sources(srcs, tmp_path / "m.epub", fmt="epub", title="合集")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        images = [n for n in names if n.startswith("OEBPS/images/")]
    assert len(names) == len(set(names))
    assert len(images) == 2
    assert images == ["OEBPS/images/s001/0001.jpg", "OEBPS/images/s002/0001.jpg"]


def test_merged_epub_marks_a_cover(tmp_path: Path) -> None:
    a = _make_image(tmp_path / "a" / "1.jpg")
    b = _make_image(tmp_path / "b" / "1.jpg")
    srcs = [
        BookSource(path=a, title="甲", kind="cbz", images=[("1.jpg", a.read_bytes(), 400, 600)]),
        BookSource(path=b, title="乙", kind="cbz", images=[("1.jpg", b.read_bytes(), 400, 600)]),
    ]
    out = merge_sources(srcs, tmp_path / "m.epub", fmt="epub", title="合集")
    with zipfile.ZipFile(out) as zf:
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
    assert opf.count('properties="cover-image"') == 1   # exactly one cover
    assert '<meta name="cover"' in opf


def test_source_toc_entries_point_at_their_own_pages(tmp_path: Path) -> None:
    """A source book's TOC must remap onto the right pages of the collection."""
    from nyavview.engines.ebook import EpubDoc, EpubTocEntry, write_epub

    # A two-page book whose TOC has one entry per page.
    folder = tmp_path / "src"
    p1 = _make_image(folder / "1.jpg", colour=(200, 100, 100))
    p2 = _make_image(folder / "2.jpg", colour=(100, 100, 200))
    from nyavview.engines.ebook import _EPUB_PAGE, _image_media_type

    docs = []
    for i, page in enumerate([p1, p2], 1):
        docs.append(
            EpubDoc(
                title=f"p{i}",
                body=_EPUB_PAGE.format(title=f"p{i}", w=400, h=600, src=f"images/{i:04d}.jpg"),
                assets=[(f"images/{i:04d}.jpg", page.read_bytes(), _image_media_type(f"{i}.jpg"))],
            )
        )
    book = write_epub(
        tmp_path / "two.epub",
        title="两页",
        docs=docs,
        toc=[EpubTocEntry("第一页", "page0001.xhtml"), EpubTocEntry("第二页", "page0002.xhtml")],
    )

    source = read_book_source(book)
    assert [e.title for e in source.toc] == ["第一页", "第二页"]
    assert [e.page_index for e in source.toc] == [0, 1]  # not both 0

    # And after merging, the nested links keep pointing at distinct pages.
    out = merge_sources([source], tmp_path / "merged.epub", fmt="epub", title="合集")
    with zipfile.ZipFile(out) as zf:
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    assert "第一页" in nav and "第二页" in nav
    assert 'href="page0001.xhtml">第一页' in nav
    assert 'href="page0002.xhtml">第二页' in nav


# ---------------------------------------------- queue: merge job labelling

def test_progress_events_carry_the_job_kind_not_the_engine_flavour(tmp_path: Path) -> None:
    """Regression: a merge job runs on EbookBuilder, whose progress events say
    "ebook", so the queue badge showed 电子书 instead of 合集."""
    import sys
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from nyavview.ui.queue import JobManager

    src = tmp_path / "book.txt"
    src.write_text("第一章 起\n甲\n第二章 承\n乙\n", encoding="utf-8")
    manager = JobManager()
    manager.start()
    try:
        job_id = manager.submit_merge([str(src)], {"destination": str(tmp_path / "out"),
                                                  "format": "epub", "title": "合集", "overwrite": True})
        deadline = time.time() + 30
        kinds = set()
        while time.time() < deadline:
            for event in manager.poll():
                if event.get("jobId") == job_id and event.get("type") == "progress":
                    kinds.add(event.get("kind"))
            jobs = {j["id"]: j for j in manager.list_jobs()}
            if jobs.get(job_id, {}).get("status") in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        for event in manager.poll():
            if event.get("jobId") == job_id and event.get("type") == "progress":
                kinds.add(event.get("kind"))
        assert kinds == {"merge"}, kinds
        assert jobs[job_id]["kind"] == "merge"
    finally:
        manager.stop()


def test_preface_before_the_first_heading_is_not_named_after_the_book() -> None:
    """Title page / epigraph text before the first heading must not repeat the
    book name in the TOC."""
    text = "The Call of Cthulhu  译者：竹子\n（原件见于…）\nI.粘土中的恐怖\n正文甲\nII.来自海洋的疯狂\n正文乙\n"
    chapters = split_text_chapters(text, book_title="克苏鲁的呼唤")
    assert [c[0] for c in chapters] == ["前言", "I.粘土中的恐怖", "II.来自海洋的疯狂"]


@pytest.mark.asyncio
async def test_merged_txt_toc_has_no_repeated_placeholder_entries(tmp_path: Path) -> None:
    """Regression: merging heading-less TXTs produced one "第 1 部分" per book."""
    files = []
    for name in ["大衮", "异乡人", "记忆"]:
        path = tmp_path / f"{name}.txt"
        path.write_text(f"Dagon\n这是{name}的正文内容，没有任何章节标题。\n", encoding="utf-8")
        files.append(path)
    out = await _engine().merge(files, tmp_path / "out", fmt="epub", title="合集")
    with zipfile.ZipFile(out[0]) as zf:
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    titles = re.findall(r'<a href="[^"]+">([^<]+)</a>', nav)
    assert "部分" not in nav
    assert titles == ["大衮", "异乡人", "记忆"]   # book names, no duplicated children
    assert len(titles) == len(set(titles))
