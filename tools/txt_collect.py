#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""txt_collect —— 书库 TXT 汇总整理（复制 + 清理孤儿阅读进度目录）。

配合 txt_dedupe.py 使用：先用它查重，再用本工具把书汇总到一个干净目录。

两个动作，都安全可预览
----------------------
* **汇总**：递归找出目录下所有 txt，复制到目标目录（可平铺，也可保留原目录结构）。
  默认只预览，加 ``--apply`` 才真正复制；复制后逐个校验字节数。
* **清理孤儿（可选）**：阅读器（如 Calibre）会为每本书生成同名 ``.sdr`` 进度目录。
  书被删掉后这些目录会残留。``--clean-orphan-sdr`` 会删除「没有同名 txt」的 .sdr 目录。
  删除前对每个路径做 4 项断言（是目录 / 以 .sdr 结尾 / 严格位于源目录之下 / 同名 txt
  确实不存在），并再校验一次才删。

用法
----
    # 1) 预览：会复制哪些、会删哪些（不改动任何文件）
    python txt_collect.py "F:\\书库" "F:\\书库-汇总"

    # 2) 真正执行（平铺）
    python txt_collect.py "F:\\书库" "F:\\书库-汇总" --apply

    # 3) 保留作者/系列子目录结构
    python txt_collect.py "F:\\书库" "F:\\书库-汇总" --apply --keep-structure

    # 4) 同时清理孤儿 .sdr 进度目录
    python txt_collect.py "F:\\书库" "F:\\书库-汇总" --apply --clean-orphan-sdr
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

TEXT_EXTS = {".txt", ".md"}
SDR_SUFFIX = ".sdr"


def find_texts(root: Path) -> list[Path]:
    return sorted(
        [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_EXTS],
        key=lambda p: str(p),
    )


def find_orphan_sdr(root: Path, texts: list[Path]) -> tuple[list[Path], list[Path]]:
    """返回（孤儿 .sdr 目录, 校验未通过的路径）。"""
    book_stems = {p.with_suffix("") for p in texts}
    orphans: list[Path] = []
    rejected: list[Path] = []
    for candidate in sorted(root.rglob(f"*{SDR_SUFFIX}")):
        if not candidate.is_dir():
            continue
        if candidate.with_suffix("") in book_stems:
            continue                      # 书还在，不是孤儿
        resolved = candidate.resolve()
        ok = (
            resolved.is_dir()                                   # 1) 是目录
            and resolved.name.endswith(SDR_SUFFIX)              # 2) 以 .sdr 结尾
            and root.resolve() in resolved.parents              # 3) 严格位于源目录之下
            and not resolved.with_suffix("").is_file()          # 4) 同名书确实不在
        )
        (orphans if ok else rejected).append(resolved)
    return orphans, rejected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="txt_collect",
        description="书库 TXT 汇总整理（复制到新目录；可选清理孤儿 .sdr 进度目录），默认只预览",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法", 1)[-1],
    )
    parser.add_argument("source", help="源目录（递归扫描）")
    parser.add_argument("dest", help="目标目录（不存在会自动创建）")
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    parser.add_argument("--keep-structure", action="store_true", help="保留原目录结构（默认平铺）")
    parser.add_argument("--clean-orphan-sdr", action="store_true", help="删除没有同名书的 .sdr 目录")
    parser.add_argument("--manifest", metavar="清单.md", help="把复制清单写入文件（默认写到目标目录）")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    dest = Path(args.dest).expanduser().resolve()
    if not source.is_dir():
        print(f"源目录不存在：{source}", file=sys.stderr)
        return 2
    if dest == source or source in dest.parents or dest in source.parents:
        print(f"目标目录不能是源目录本身或其父子目录：{dest}", file=sys.stderr)
        return 2

    texts = find_texts(source)
    if not texts:
        print(f"{source} 里没有 .txt / .md 文件", file=sys.stderr)
        return 2

    print(f"源目录：{source}")
    print(f"目标目录：{dest}")
    print(f"待复制：{len(texts)} 个文本文件")

    # work out the final target name for every file (handles collisions up front)
    planned: list[tuple[Path, Path]] = []
    used: set[str] = set()
    renamed: list[tuple[str, str]] = []
    for path in texts:
        if args.keep_structure:
            target = dest / path.relative_to(source)
            key = str(target.relative_to(dest))
            if key in used:
                continue
        else:
            name = path.name
            if name in used:
                stem, suffix = path.stem, path.suffix
                n = 1
                while f"{stem} ({n}){suffix}" in used:
                    n += 1
                renamed.append((name, f"{stem} ({n}){suffix}"))
                name = f"{stem} ({n}){suffix}"
            used.add(name)
            target = dest / name
        planned.append((path, target))

    if renamed:
        print(f"  同名文件自动改名：{len(renamed)} 个")

    orphans, rejected = ([], [])
    if args.clean_orphan_sdr:
        orphans, rejected = find_orphan_sdr(source, texts)
        print(f"\n孤儿 .sdr 目录（同名书已不在）：{len(orphans)} 个")
        for item in orphans:
            print(f"  - {item.relative_to(source)}")
        if rejected:
            print(f"  ! 校验未通过、将跳过：{len(rejected)} 个", file=sys.stderr)

    if not args.apply:
        print("\n以上为预览，未改动任何文件。确认后加 --apply 执行。")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path, target in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if target.stat().st_size != path.stat().st_size:
            print(f"  ! 字节数不一致：{path}", file=sys.stderr)
            return 1
        copied += 1
    print(f"\n已复制 {copied} 个（字节数全部校验通过）")

    manifest = Path(args.manifest) if args.manifest else dest / "_复制清单.md"
    lines = [
        "# 汇总复制清单",
        f"# 源：{source}",
        f"# 目标：{dest}",
        f"# 共 {len(planned)} 个文件",
        "",
    ]
    lines += [f"{target.name if not args.keep_structure else target.relative_to(dest)}\t<- {path.relative_to(source)}"
              for path, target in planned]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"清单：{manifest}")

    if args.clean_orphan_sdr:
        deleted: list[str] = []
        failed: list[str] = []
        for item in orphans:
            # 删除前再校验一次
            if not (item.is_dir() and item.name.endswith(SDR_SUFFIX) and source in item.parents):
                failed.append(str(item))
                continue
            try:
                shutil.rmtree(item)
                deleted.append(str(item.relative_to(source)))
            except OSError as exc:
                failed.append(f"{item} ({exc})")
        print(f"已删除孤儿 .sdr：{len(deleted)} 个")
        if failed:
            print(f"删除失败 {len(failed)} 个：{failed}", file=sys.stderr)
        (dest / "_孤儿目录清单.md").write_text(
            "# 已删除的孤儿 .sdr 目录（同名书已不在）\n"
            "# 这些只是阅读进度元数据，删除不影响书本身\n\n" + "\n".join(deleted) + "\n",
            encoding="utf-8",
        )
        return 1 if failed else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
