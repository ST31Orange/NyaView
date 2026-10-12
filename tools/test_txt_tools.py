#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""txt_dedupe / txt_collect 的自测（在临时目录造书库，跑完整流程）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEDUPE = HERE / "txt_dedupe.py"
COLLECT = HERE / "txt_collect.py"

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         期望: {want}")
        print(f"         实际: {got}")
        failures += 0 if ok else 1


def run(script: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def make_library(root: Path) -> Path:
    """造一个书库：同一本书的两个版本（文件名不同、一个更全）+ 一本不同的书 + 孤儿 .sdr。"""
    lib = root / "lib"
    (lib / "作者A").mkdir(parents=True)

    # 同一本书的两个版本 —— 文件名不同、开头头部不同、正文一致但一版更长
    body = "我是在精神明显紧张的状态下撰写此文的。因为到明晚我将不复存在。" * 12
    (lib / "Dagon 大衮.txt").write_bytes(
        ("Dagon  实体书\n" + body).encode("gb18030"))                    # GB18030 短版
    (lib / "作者A" / "大衮（完整版）.txt").write_bytes(
        ("The Dagon  译者：竹子\n" + body + "结尾补充内容。" * 40).encode("utf-8"))  # UTF-8 长版

    # 一本完全不同的书
    (lib / "作者A" / "异乡人.txt").write_text(
        "The Outsider  译者：竹子\n若一个人孩童时的记忆带给他的仅仅只有恐惧和悲伤，那么他是不幸的。\n",
        encoding="utf-8")

    # 阅读进度目录：一本有书（正常），一本是孤儿（书已删）
    (lib / "Dagon 大衮.sdr").mkdir()
    (lib / "Dagon 大衮.sdr" / "x.mbp1").write_bytes(b"pos")
    (lib / "作者A" / "已删除的书.sdr").mkdir()
    (lib / "作者A" / "已删除的书.sdr" / "y.mbp1").write_bytes(b"pos")
    return lib


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lib = make_library(root)

        print("\n1) txt_dedupe 查重报告（只读）")
        before = sorted(p.name for p in lib.rglob("*"))
        code, out = run(DEDUPE, str(lib), "--json", str(root / "r.json"))
        check("退出码 0", code, 0)
        report = json.loads((root / "r.json").read_text(encoding="utf-8"))
        check("扫描到 3 个文件", report["total"], 3)
        check("发现 1 组重复", len(report["groups"]), 1)
        check("保留内容最全的版本（完整版）", "完整版" in report["groups"][0]["keep"], True)
        check("丢弃较短版本", len(report["groups"][0]["drop"]), 1)
        check("查重未改动文件", sorted(p.name for p in lib.rglob("*")), before)

        print("\n2) 跨编码识别（GB18030 vs UTF-8 判为同一本）")
        check("识别出英文原题", report["files"][0]["title"] not in ("", None), True)

        print("\n3) txt_dedupe --apply 输出去重结果")
        outdir = root / "deduped"
        code, out = run(DEDUPE, str(lib), "--apply", "--out", str(outdir))
        check("退出码 0", code, 0)
        got = sorted(p.name for p in outdir.rglob("*.txt"))
        check("去重后只剩 2 本（长的版本 + 异乡人）", len(got), 2)
        check("保留了完整版", any("完整版" in n for n in got), True)
        check("丢弃了短版", any(n.startswith("Dagon 大衮") for n in got), False)

        print("\n4) txt_collect 预览不应改动任何文件")
        dest = root / "collected"
        code, out = run(COLLECT, str(lib), str(dest))
        check("退出码 0", code, 0)
        check("预览未创建目标目录", dest.exists(), False)
        check("预览不动源目录", sorted(p.name for p in lib.rglob("*")), before)

        print("\n5) txt_collect --apply 平铺汇总")
        code, out = run(COLLECT, str(lib), str(dest), "--apply")
        check("退出码 0", code, 0)
        got = sorted(p.name for p in dest.rglob("*.txt"))
        check("3 个文件全部平铺复制", len(got), 3)
        check("无子目录（平铺）", sum(1 for p in dest.iterdir() if p.is_dir()), 0)
        check("生成了复制清单", (dest / "_复制清单.md").is_file(), True)
        check("源目录完好", sorted(p.name for p in lib.rglob("*")), before)
        # content check
        same = all(
            any((dest / p.name).read_bytes() == p.read_bytes() for p in lib.rglob(p.name))
            for p in lib.rglob("*.txt")
        )
        check("复制内容逐字节一致", same, True)

        print("\n6) txt_collect 保留目录结构")
        dest2 = root / "collected2"
        code, out = run(COLLECT, str(lib), str(dest2), "--apply", "--keep-structure")
        check("退出码 0", code, 0)
        check("保留了作者子目录", (dest2 / "作者A" / "异乡人.txt").is_file(), True)

        print("\n7) 清理孤儿 .sdr（有书的保留，没书的删除）")
        dest3 = root / "collected3"
        code, out = run(COLLECT, str(lib), str(dest3), "--apply", "--clean-orphan-sdr")
        check("退出码 0", code, 0)
        check("孤儿目录已删除", (lib / "作者A" / "已删除的书.sdr").exists(), False)
        check("有书的 .sdr 保留", (lib / "Dagon 大衮.sdr").is_dir(), True)
        check("书的 txt 一个没少", len(list(lib.rglob("*.txt"))), 3)
        check("生成了孤儿清单", (dest3 / "_孤儿目录清单.md").is_file(), True)

        print("\n8) 目标目录是源的子目录时应拒绝")
        code, out = run(COLLECT, str(lib), str(lib / "sub"), "--apply")
        check("拒绝并返回非 0", code != 0, True)

    print(f"\n{'=' * 46}\n{'全部通过' if failures == 0 else f'{failures} 项失败'}\n{'=' * 46}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
