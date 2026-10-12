#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore_manga_names —— 把 Cimoc 漫画库里的乱码文件夹名还原成章节名。

背景
----
Cimoc（安卓漫画阅读器）把每一话下载成 `m<数字>` 形式的文件夹，真正的章节名
存在索引文件 `index.cdif` 里。索引文件用 5 字节魔数 ``cimoc`` 开头，后面跟一段
JSON：

    漫画级（type=comic）：{"version":"1","type":"comic","title":"亚人",
                          "list":[{"title":"第75话","path":"m959104"}, ...]}
    章节级（type=chapter）：{"version":"1","type":"chapter",
                            "title":"第1卷","path":"m426948"}

本工具读取这两份索引，把 `m959104` 这类文件夹改名为 `第75话`。

安全设计
--------
* **默认只预览，不改动任何东西**；确认真实改动要显式加 ``--apply``。
* 每次改动都会写一份 **撤销日志**，可用 ``--undo 日志.json`` 一键还原。
* 目标名已存在时默认跳过，绝不覆盖（除非 ``--force``）。
* 两份索引不一致时会列出冲突，默认以漫画级索引为准。
* 全部使用标准库，单文件即可运行。

用法
----
    # 1) 预览（推荐先跑这个）
    python restore_manga_names.py "F:\\[AmberNya]__[漫画]\\亚人漫画"

    # 2) 真正执行
    python restore_manga_names.py "F:\\[AmberNya]__[漫画]\\亚人漫画" --apply

    # 3) 撤销
    python restore_manga_names.py --undo "F:\\...\\亚人漫画\\.rename-log-20261009-120000.json"

    # 4) 批量处理某目录下所有漫画（自动找 index.cdif）
    python restore_manga_names.py --batch "F:\\[AmberNya]__[漫画]" --apply

    # 5) 保留原始 ID 便于对照，并给文件名加阅读顺序前缀
    python restore_manga_names.py "<漫画目录>" --apply --keep-id --num
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

MAGIC = b"cimoc"
INDEX_NAMES = ("index.cdif",)
LOG_PREFIX = ".rename-log-"

# Windows 文件名非法字符 + 保留名
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------------- 索引读写


def read_index(path: Path) -> dict:
    """读一个 .cdif 并返回其中的 JSON（自动跳过 cimoc 魔数）。"""
    raw = path.read_bytes()
    body = raw[len(MAGIC):] if raw.startswith(MAGIC) else raw
    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise ValueError(f"{path} 内容为空")
    return json.loads(text)


def write_index(path: Path, data: dict) -> None:
    """按 cimoc 格式写回 .cdif（魔数 + 紧凑 JSON）。"""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_bytes(MAGIC + body.encode("utf-8"))


def find_index(folder: Path) -> Path | None:
    for name in INDEX_NAMES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- 映射收集


def collect_mapping(manga_dir: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """收集 path -> title 映射。

    返回（漫画级索引映射, 各话自带索引映射, 提示信息）。
    """
    notes: list[str] = []
    from_comic: dict[str, str] = {}
    from_chapter: dict[str, str] = {}

    index = find_index(manga_dir)
    if index is not None:
        try:
            data = read_index(index)
        except Exception as exc:  # 索引坏了不该让整个工具挂掉
            notes.append(f"读取 {index.name} 失败：{exc}")
        else:
            if data.get("type") == "comic" and isinstance(data.get("list"), list):
                for entry in data["list"]:
                    path = entry.get("path")
                    title = entry.get("title")
                    if path and title:
                        from_comic[str(path)] = str(title)
            elif data.get("type") == "chapter" and data.get("path") and data.get("title"):
                # 目录本身就是一话（没有子文件夹）
                from_chapter[str(data["path"])] = str(data["title"])
            else:
                notes.append(f"{index.name} 里没有可用的 list（type={data.get('type')!r}）")

    # 各话自己的 index.cdif（可作为补充或校验）
    for child in sorted(manga_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        own = find_index(child)
        if own is None:
            continue
        try:
            data = read_index(own)
        except Exception:
            continue
        if data.get("type") == "chapter" and data.get("title"):
            from_chapter[child.name] = str(data["title"])

    return from_comic, from_chapter, notes


# --------------------------------------------------------------------------- 名字处理


def safe_name(title: str) -> str:
    """把标题变成 Windows 上合法的文件夹名。"""
    name = _ILLEGAL.sub("_", title).strip().rstrip(". ")
    if not name:
        name = "_"
    if name.upper() in _RESERVED:
        name += "_"
    return name


def reading_order(title: str) -> tuple:
    """给标题算一个阅读顺序键：卷 → 话 → 外传，各自按数字排。"""
    text = title.strip()
    family = 1
    if text.startswith("外传") or text.startswith("番外"):
        family = 2
        text = re.sub(r"^(外传|番外)[：:\s]*", "", text)
    elif "卷" in text:
        family = 0

    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return (3, family, 0.0, 0.0, title)
    major = numbers[0]
    minor = numbers[1] if len(numbers) > 1 else 0.0
    return (family, 0, major, minor, title)


# --------------------------------------------------------------------------- 计划与执行


def build_plan(
    manga_dir: Path,
    *,
    source: str = "auto",
    keep_id: bool = False,
    number: bool = False,
) -> tuple[list[dict], list[str]]:
    """生成重命名计划（不落盘）。返回（计划项, 提示信息）。"""
    from_comic, from_chapter, notes = collect_mapping(manga_dir)

    if source == "parent":
        merged = dict(from_comic)
    elif source == "chapter":
        merged = dict(from_chapter)
    else:  # auto：漫画级优先，缺失时用该话自带索引补齐
        merged = {**from_chapter, **from_comic} if from_comic else dict(from_chapter)

    conflicts = sorted(
        name for name in set(from_comic) & set(from_chapter)
        if from_comic[name] != from_chapter[name]
    )
    if conflicts:
        notes.append(f"{len(conflicts)} 个文件夹的两份索引标题不一致（以漫画级索引为准）")

    folders = sorted((p for p in manga_dir.iterdir() if p.is_dir()), key=lambda p: p.name)

    # 阅读顺序前缀：按内容排序后编号（与文件夹的乱码名无关）
    ordered = sorted(folders, key=lambda p: reading_order(merged.get(p.name, p.name)))
    rank = {p.name: i + 1 for i, p in enumerate(ordered)} if number else {}

    plan: list[dict] = []
    for folder in folders:
        title = merged.get(folder.name)
        if not title:
            plan.append({"folder": folder, "old": folder.name, "new": None, "why": "索引里没有对应标题"})
            continue
        new = safe_name(title)
        if keep_id:
            new = f"{new} [{folder.name}]"
        if number and folder.name in rank:
            new = f"{rank[folder.name]:03d} {new}"
        plan.append({"folder": folder, "old": folder.name, "new": new, "title": title, "why": ""})

    return plan, notes


def apply_plan(plan: list[dict], *, force: bool = False, log_path: Path | None = None) -> dict:
    """执行重命名，返回统计结果。"""
    done: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    for item in plan:
        new = item["new"]
        if not new:
            skipped.append({**item, "reason": item["why"]})
            continue
        if new == item["old"]:
            skipped.append({**item, "reason": "名字已是目标名"})
            continue

        target = item["folder"].parent / new
        if target.exists():
            if not force:
                skipped.append({**item, "reason": f"目标已存在：{new}"})
                continue
            failed.append({**item, "reason": f"目标已存在且 --force：{new}"})
            continue

        try:
            item["folder"].rename(target)
        except OSError as exc:
            failed.append({**item, "reason": str(exc)})
        else:
            done.append({"old": item["old"], "new": new, "title": item.get("title", "")})

    if log_path is not None and done:
        log_path.write_text(
            json.dumps(
                {"manga_dir": str(plan[0]["folder"].parent) if plan else "", "renames": done},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return {"done": done, "skipped": skipped, "failed": failed}


def undo_log(log_path: Path) -> tuple[int, list[str]]:
    """按撤销日志把文件夹改回原名。"""
    data = json.loads(Path(log_path).read_text(encoding="utf-8"))
    manga_dir = Path(data["manga_dir"])
    errors: list[str] = []
    count = 0
    for entry in reversed(data["renames"]):  # 逆序还原，避免中间态冲突
        current = manga_dir / entry["new"]
        original = manga_dir / entry["old"]
        if not current.exists():
            errors.append(f"找不到 {entry['new']}（可能已手动改名）")
            continue
        if original.exists():
            errors.append(f"原名 {entry['old']} 已被占用，跳过")
            continue
        try:
            current.rename(original)
            count += 1
        except OSError as exc:
            errors.append(f"{entry['new']} -> {entry['old']} 失败：{exc}")
    return count, errors


def update_index_paths(manga_dir: Path, plan: list[dict], *, apply: bool) -> tuple[int, Path | None]:
    """把漫画级 index.cdif 里的 path 改成新的文件夹名，让 Cimoc 仍能打开。"""
    index = find_index(manga_dir)
    if index is None:
        return 0, None
    data = read_index(index)
    if not isinstance(data.get("list"), list):
        return 0, None

    rename_map = {i["old"]: i["new"] for i in plan if i["new"] and i["new"] != i["old"]}
    changed = 0
    for entry in data["list"]:
        new = rename_map.get(entry.get("path"))
        if new:
            entry["path"] = new
            changed += 1

    if not changed:
        return 0, None

    backup = None
    if apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = index.with_name(f"{index.name}.bak-{stamp}")
        backup.write_bytes(index.read_bytes())
        write_index(index, data)
    return changed, backup


# --------------------------------------------------------------------------- 输出


def print_plan(manga_dir: Path, plan: list[dict], notes: list[str], *, applied: bool) -> None:
    ok = [i for i in plan if i["new"] and i["new"] != i["old"]]
    same = [i for i in plan if i["new"] == i["old"]]
    missing = [i for i in plan if not i["new"]]

    print(f"\n漫画目录：{manga_dir}")
    print(f"子文件夹：{len(plan)} 个 | 待改名：{len(ok)} | 已是目标名：{len(same)} | 无标题：{len(missing)}")

    width = max((len(i["old"]) for i in plan), default=4)
    for item in plan:
        old = item["old"]
        if not item["new"]:
            print(f"  {old:<{width}}  ->  （跳过）{item['why']}")
        elif item["new"] == old:
            print(f"  {old:<{width}}  =  {old}（已是目标名）")
        else:
            print(f"  {old:<{width}}  ->  {item['new']}")

    for note in notes:
        print(f"  ! {note}")

    if not applied and ok:
        print(f"\n以上为预览，未改动任何文件。确认后加 --apply 执行。")


# --------------------------------------------------------------------------- 主流程


def process_one(manga_dir: Path, args) -> int:
    plan, notes = build_plan(
        manga_dir, source=args.source, keep_id=args.keep_id, number=args.num
    )

    if not plan:
        print(f"\n漫画目录：{manga_dir}\n  没有子文件夹，跳过。")
        return 0

    print_plan(manga_dir, plan, notes, applied=args.apply)

    if not args.apply:
        # 预览时也展示索引会怎么改，但不落盘
        if args.update_index:
            changed, _ = update_index_paths(manga_dir, plan, apply=False)
            if changed:
                print(f"  （--update-index）将同步更新 index.cdif 里 {changed} 条 path")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = manga_dir / f"{LOG_PREFIX}{stamp}.json"
    result = apply_plan(plan, force=args.force, log_path=log_path)

    print(f"\n已改名 {len(result['done'])} 个，跳过 {len(result['skipped'])} 个，失败 {len(result['failed'])} 个")
    # 「名字已是目标名」是正常情况，其余跳过（尤其是目标已存在）必须让用户看见
    for item in result["skipped"]:
        if item["reason"] != "名字已是目标名":
            print(f"  - {item['old']}：{item['reason']}")
    for item in result["failed"]:
        print(f"  x {item['old']}：{item['reason']}")
    if result["done"]:
        print(f"  撤销日志：{log_path}")

    if args.update_index:
        changed, backup = update_index_paths(manga_dir, plan, apply=True)
        if changed:
            print(f"  已同步 index.cdif 的 {changed} 条 path" + (f"，备份：{backup.name}" if backup else ""))

    return 1 if result["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="restore_manga_names",
        description="把 Cimoc 漫画库里 m<数字> 形式的乱码文件夹名还原成章节名（默认只预览）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法", 1)[-1] if "用法" in __doc__ else "",
    )
    parser.add_argument("manga_dir", nargs="?", help="漫画目录（里面有 index.cdif 和 m<数字> 文件夹）")
    parser.add_argument("--apply", action="store_true", help="真正执行改名（默认只预览）")
    parser.add_argument("--batch", metavar="父目录", help="批量处理该目录下所有含 index.cdif 的漫画")
    parser.add_argument("--undo", metavar="日志.json", help="按撤销日志还原文件名")
    parser.add_argument("--source", choices=("auto", "parent", "chapter"), default="auto",
                        help="标题来源：auto=漫画级优先(默认)，parent=只用漫画级，chapter=只用各话自带")
    parser.add_argument("--keep-id", action="store_true", help="保留原始 ID，如 `第1卷 [m426948]`")
    parser.add_argument("--num", action="store_true", help="加 3 位阅读顺序前缀，如 `001 第1卷`")
    parser.add_argument("--update-index", action="store_true", help="同步更新 index.cdif 的 path（自动备份）")
    parser.add_argument("--force", action="store_true", help="目标已存在时也不跳过（危险，一般别用）")
    args = parser.parse_args(argv)

    if args.undo:
        count, errors = undo_log(Path(args.undo))
        print(f"已还原 {count} 个文件夹")
        for err in errors:
            print(f"  ! {err}")
        return 1 if errors else 0

    targets: list[Path] = []
    if args.batch:
        parent = Path(args.batch)
        if not parent.is_dir():
            print(f"目录不存在：{parent}", file=sys.stderr)
            return 2
        targets = [d for d in sorted(parent.iterdir()) if d.is_dir() and find_index(d)]
        if not targets:
            print(f"{parent} 下没有找到包含 index.cdif 的漫画目录", file=sys.stderr)
            return 2
        print(f"批量模式：找到 {len(targets)} 部漫画")
    elif args.manga_dir:
        targets = [Path(args.manga_dir)]
    else:
        parser.error("请给出漫画目录，或用 --batch 父目录 / --undo 日志.json")

    exit_code = 0
    for target in targets:
        if not target.is_dir():
            print(f"目录不存在：{target}", file=sys.stderr)
            exit_code = 2
            continue
        exit_code |= process_one(target, args)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
