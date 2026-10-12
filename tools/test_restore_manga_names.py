#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore_manga_names 的自测：在临时目录造一个 Cimoc 漫画库，跑完整流程。

验证：预览不改动 / --apply 正确改名 / 两份索引冲突处理 / --keep-id / --num /
       --undo 完整还原 / 目标已存在时跳过 / 批量模式。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = Path(__file__).with_name("restore_manga_names.py")
MAGIC = b"cimoc"


def write_cdif(path: Path, data: dict) -> None:
    path.write_bytes(MAGIC + json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def make_manga(root: Path) -> Path:
    """造一部漫画：3 卷 + 3 话，另加一个索引里没有的文件夹。"""
    manga = root / "亚人漫画"
    manga.mkdir(parents=True)
    entries = [
        ("m100", "第1卷"),
        ("m101", "第2卷"),
        ("m102", "第75话"),
        ("m103", "第74.5话"),
    ]
    for folder, title in entries + [("m999", None)]:
        d = manga / folder
        d.mkdir()
        (d / "001.jpg").write_bytes(b"fake")
        if title:
            write_cdif(d / "index.cdif", {"version": "1", "type": "chapter", "title": title, "path": folder})
    write_cdif(
        manga / "index.cdif",
        {
            "version": "1",
            "type": "comic",
            "source": 5,
            "cid": "manhua-yaren",
            "title": "亚人",
            "list": [{"title": t, "path": p} for p, t in entries],
        },
    )
    return manga


def run(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def dirs(manga: Path) -> list[str]:
    return sorted(p.name for p in manga.iterdir() if p.is_dir())


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         期望: {want}")
        print(f"         实际: {got}")
    return ok


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manga = make_manga(root)

        print("\n1) 预览不应改动任何东西")
        before = dirs(manga)
        code, out = run(str(manga))
        failures += not check("预览后目录不变", dirs(manga), before)
        failures += not check("预览输出含待改名提示", "第1卷" in out and "--apply" in out, True)
        failures += not check("预览不提无标题文件夹", "m999" in out, True)

        print("\n2) --apply 按索引改名")
        code, out = run(str(manga), "--apply")
        failures += not check("改名后的目录", dirs(manga), sorted(["第1卷", "第2卷", "第74.5话", "第75话", "m999"]))
        failures += not check("退出码为 0", code, 0)

        print("\n3) 撤销日志已生成且能完整还原")
        logs = sorted(manga.glob(".rename-log-*.json"))
        failures += not check("生成撤销日志", len(logs), 1)
        if logs:
            code, out = run("--undo", str(logs[0]))
            failures += not check("还原后目录", dirs(manga), before)
            failures += not check("撤销退出码为 0", code, 0)

        print("\n4) 索引冲突时以漫画级为准")
        write_cdif(manga / "m102" / "index.cdif",
                   {"version": "1", "type": "chapter", "title": "错误标题", "path": "m102"})
        code, out = run(str(manga))
        failures += not check("提示存在冲突", "不一致" in out, True)
        failures += not check("采用漫画级标题", "第75话" in out and "错误标题" not in out, True)

        print("\n5) 目标名已存在时跳过而不覆盖")
        (manga / "第1卷").mkdir()  # 手工占位
        code, out = run(str(manga), "--apply")
        failures += not check("占位目录仍在", (manga / "第1卷").is_dir(), True)
        failures += not check("m100 未被改名", (manga / "m100").is_dir(), True)
        failures += not check("提示跳过", "目标已存在" in out, True)
        (manga / "第1卷").rmdir()

        print("\n6) --keep-id + --update-index（同一次执行）")
        for log in manga.glob(".rename-log-*.json"):
            run("--undo", str(log))
            log.unlink()
        for bak in manga.glob("index.cdif.bak-*"):
            bak.unlink()
        code, out = run(str(manga), "--apply", "--keep-id", "--update-index")
        failures += not check("保留 ID", (manga / "第1卷 [m100]").is_dir(), True)
        index = json.loads((manga / "index.cdif").read_bytes()[5:].decode("utf-8"))
        paths = [e["path"] for e in index["list"]]
        failures += not check(
            "index 的 path 已更新",
            paths,
            ["第1卷 [m100]", "第2卷 [m101]", "第75话 [m102]", "第74.5话 [m103]"],
        )
        failures += not check("生成了索引备份", len(list(manga.glob("index.cdif.bak-*"))) >= 1, True)

        print("\n7) --num 加阅读顺序前缀（卷在前、话在后、复数按数值）")
        for log in manga.glob(".rename-log-*.json"):
            run("--undo", str(log))
            log.unlink()
        for bak in manga.glob("index.cdif.bak-*"):
            bak.unlink()
        write_cdif(manga / "index.cdif", {
            "version": "1", "type": "comic", "title": "亚人",
            "list": [{"title": t, "path": p} for p, t in
                     [("m100", "第1卷"), ("m101", "第2卷"), ("m102", "第75话"), ("m103", "第74.5话")]],
        })
        code, out = run(str(manga), "--apply", "--num")
        failures += not check(
            "阅读顺序前缀",
            dirs(manga),
            sorted(["001 第1卷", "002 第2卷", "003 第74.5话", "004 第75话", "m999"]),
        )

        print("\n8) --batch 批量处理")
        code, out = run("--batch", str(root))
        failures += not check("批量能找到漫画", "找到 1 部漫画" in out, True)

    print(f"\n{'=' * 46}\n{'全部通过' if failures == 0 else f'{failures} 项失败'}\n{'=' * 46}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
