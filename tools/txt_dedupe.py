#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""txt_dedupe —— 书库 TXT 内容查重（找同一本书的不同版本）。

为什么不能只看文件名
--------------------
同一本书的不同版本往往文件名不同、大小不同；而中文小说的文本开头常带
「英文原题 + 译者」头部，直接比较开头几行会被头部掩盖。本工具因此：

1. 单独比对**英文原题**（首个拉丁文片段）；
2. 跳过标题/译者等头部，**从第一段连续中文正文**开始取一段做相似度比对；
3. 相似度超过阈值即判为同一本书，并在组内**保留内容最全的那一版**。

只读；要落盘必须显式加 ``--apply``。全部标准库实现。

用法
----
    # 1) 查重报告（推荐先跑，只读不改）
    python txt_dedupe.py "F:\\书库"

    # 2) 导出 JSON 报告
    python txt_dedupe.py "F:\\书库" --json report.json

    # 3) 把去重结果复制到新目录（每组保留最全的一本，其余文件原样复制）
    python txt_dedupe.py "F:\\书库" --apply --out "F:\\书库-去重"

    # 4) 调整判定严格程度
    python txt_dedupe.py "F:\\书库" --threshold 0.7
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

TEXT_EXTS = {".txt", ".md"}
# 中文小说常见 GB18030/GBK，而 gb18030 几乎不会解码失败，所以 UTF-8 必须优先
TEXT_ENCODINGS = ("utf-8-sig", "utf-16", "gb18030", "big5", "cp1252")

_CJK = re.compile(r"[\u4e00-\u9fff]")
_KEEP = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'’,.\- ]{6,}")


def read_text(path: Path) -> str:
    """读取文本，自动识别编码。"""
    raw = path.read_bytes()
    for encoding in TEXT_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # gb18030/big5 几乎不会抛错，用替换字符比例做兜底判断
        if text.count("\ufffd") <= max(1, len(text) // 500):
            return text.replace("\r\n", "\n").replace("\r", "\n")
    return raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def latin_title(text: str) -> str:
    """首个拉丁文片段（通常是英文原题）。"""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        match = _LATIN.search(line)
        if match:
            return re.sub(r"[^A-Za-z0-9]", "", match.group(0)).lower()
        break
    match = _LATIN.search(text[:400])
    return re.sub(r"[^A-Za-z0-9]", "", match.group(0)).lower() if match else ""


def body_signature(text: str, size: int = 120) -> str:
    """从正文开始取签名：跳过标题/译者等头部。

    定位「第一段连续中文」，从那里取 ``size`` 个字符（只保留中英文与数字，
    丢掉标点空白，避免排版差异影响比对）。
    """
    flat = text.replace("\r\n", "\n")
    for match in re.finditer(
        r"[\u4e00-\u9fff][\u4e00-\u9fff\uff0c\u3002\uff01\uff1f\u201c\u201d\u3001\uff1b\uff1a\s]{15,}",
        flat,
    ):
        chunk = _KEEP.sub("", flat[match.start():match.start() + 600])
        if len(chunk) >= 40:
            return chunk[:size]
    return _KEEP.sub("", flat)[:size]


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def collect_files(root: Path) -> list[Path]:
    return sorted(
        [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_EXTS],
        key=lambda p: str(p),
    )


def find_groups(files: list[Path], root: Path, threshold: float) -> tuple[list[list[dict]], list[dict]]:
    """返回（重复组, 全部文件信息）。组内按内容多少降序，第一个即保留项。"""
    records: list[dict] = []
    for path in files:
        text = read_text(path)
        records.append(
            {
                "path": path,
                "rel": str(path.relative_to(root)),
                "name": path.name,
                "bytes": path.stat().st_size,
                "chars": len(text),
                "cjk": len(_CJK.findall(text)),
                "title": latin_title(text),
                "body": body_signature(text),
            }
        )

    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            a, b = records[i], records[j]
            # 英文原题相同 → 直接判定同书；否则比对正文签名
            same = bool(a["title"]) and a["title"] == b["title"]
            if not same:
                same = similarity(a["body"], b["body"]) >= threshold
            if same:
                union(i, j)

    buckets: dict[int, list[int]] = {}
    for index in range(len(records)):
        buckets.setdefault(find(index), []).append(index)

    groups = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        items = [records[m] for m in members]
        items.sort(key=lambda r: (-r["chars"], r["rel"]))  # 内容最全的排第一
        groups.append(items)
    groups.sort(key=lambda g: g[0]["rel"])
    return groups, records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="txt_dedupe",
        description="书库 TXT 内容查重（按正文判定同一本书的不同版本），默认只读",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法", 1)[-1],
    )
    parser.add_argument("folder", help="书库目录（递归扫描 .txt / .md）")
    parser.add_argument("--threshold", type=float, default=0.55, help="正文相似度阈值（默认 0.55）")
    parser.add_argument("--json", metavar="报告.json", help="把报告写入 JSON")
    parser.add_argument("--apply", action="store_true", help="把去重结果复制到 --out 目录")
    parser.add_argument("--out", metavar="目录", help="--apply 的输出目录")
    parser.add_argument("--flat", action="store_true", help="--apply 时平铺，不保留原目录结构")
    args = parser.parse_args(argv)

    root = Path(args.folder).expanduser()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    files = collect_files(root)
    if not files:
        print(f"{root} 里没有 .txt / .md 文件", file=sys.stderr)
        return 2

    groups, records = find_groups(files, root, args.threshold)

    print(f"目录：{root}")
    print(f"扫描：{len(records)} 个文件 | 重复组：{len(groups)}\n")

    duplicated: set[str] = set()
    for index, group in enumerate(groups, 1):
        keep = group[0]
        print(f"--- 组 {index}：保留 {keep['rel']}（{keep['chars']} 字）")
        for item in group[1:]:
            duplicated.add(item["rel"])
            print(f"      丢弃 {item['rel']}（{item['chars']} 字，{item['bytes'] / 1024:.1f} KB）")

    if groups:
        titles = Counter(r["name"] for r in records)
        same_name = {k: v for k, v in titles.items() if v > 1}
        if same_name:
            print(f"\n注意：有 {len(same_name)} 个重名文件（不同目录）")

    report = {
        "root": str(root),
        "total": len(records),
        "threshold": args.threshold,
        "groups": [
            {
                "keep": group[0]["rel"],
                "keep_chars": group[0]["chars"],
                "drop": [{"rel": i["rel"], "chars": i["chars"]} for i in group[1:]],
            }
            for group in groups
        ],
        "files": [{k: r[k] for k in ("rel", "name", "bytes", "chars", "title")} for r in records],
    }
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入：{args.json}")

    if not args.apply:
        if groups:
            print("\n以上仅为报告，未改动任何文件。加 --apply --out <目录> 可输出去重结果。")
        else:
            print("未发现重复，无需处理。")
        return 0

    # ---- apply: copy the most complete of each group plus every unique file ----
    if not args.out:
        print("\n--apply 需要同时指定 --out <目录>", file=sys.stderr)
        return 2
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    keep_paths = {group[0]["rel"] for group in groups}
    copied = 0
    used: set[str] = set()
    for record in records:
        rel = record["rel"]
        if rel in duplicated:          # 丢弃较差的版本
            continue
        if args.flat:
            target = out_dir / record["name"]
            if target.name in used:    # 平铺时的同名保护
                stem, suffix = target.stem, target.suffix
                n = 1
                while f"{stem} ({n}){suffix}" in used:
                    n += 1
                target = out_dir / f"{stem} ({n}){suffix}"
            used.add(target.name)
        else:
            target = out_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(record["path"], target)
        copied += 1

    print(f"\n已复制 {copied} 个文件到 {out_dir}")
    print(f"  其中重复组保留 {len(keep_paths)} 本，丢弃 {len(duplicated)} 本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
