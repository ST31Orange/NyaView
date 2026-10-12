# 书库整理工具

配合 NyaView 的「图片 → 电子书 / 电子书合集」使用的命令行小工具，**只用标准库**，
可以直接在任意目录运行（不需要安装依赖）。

> 这些工具是给「把书库整理干净、再交给 NyaView 转电子书」这个流程用的。
> 例如先用 `txt_dedupe.py` 查重、`txt_collect.py` 汇总，再用 NyaView 的
> 「电子书合集 / TXT」标签页把整批 TXT 合成一本带目录的 EPUB。

---

## 1. `restore_manga_names.py` —— 还原漫画乱码文件夹名

Cimoc 等漫画阅读器把每一话下载成 `m<数字>` 文件夹，真正的章节名存在索引文件
`index.cdif` 里（5 字节魔数 `cimoc` + JSON）。本工具读索引把文件夹改回 `第75话` 这样的名字。

```sh
# 预览（默认，不改动任何文件）
python restore_manga_names.py "F:\[AmberNya]__[漫画]\亚人漫画"

# 执行，并同步更新 index.cdif（自动备份），让 Cimoc 仍能打开
python restore_manga_names.py "<漫画目录>" --apply --update-index

# 撤销（每次改动都会写撤销日志）
python restore_manga_names.py --undo "<漫画目录>\.rename-log-20261010-154734.json"

# 批量处理一个漫画库
python restore_manga_names.py --batch "F:\[AmberNya]__[漫画]" --apply
```

| 参数 | 作用 |
|---|---|
| `--apply` | 真正改名（默认只预览）|
| `--update-index` | 同步更新 `index.cdif` 里的 path（自动备份）|
| `--undo 日志.json` | 按撤销日志还原 |
| `--source auto/parent/chapter` | 标题来源：漫画级索引 / 各话自带索引 |
| `--keep-id` | 保留原始 ID，如 `第1卷 [m426948]` |
| `--num` | 加 3 位阅读顺序前缀，如 `001 第1卷` |
| `--force` | 目标已存在时也不跳过（危险）|

自测：`python test_restore_manga_names.py`（9 组 20 项断言，含撤销与冲突处理）

---

## 2. `txt_dedupe.py` —— 书库 TXT 内容查重

找出**同一本书的不同版本**（不只是文件名重复）。

中文小说的文本开头常是「英文原题 + 译者」，直接比较开头会被头部掩盖，所以本工具：

1. 单独比对**英文原题**（首个拉丁文片段）；
2. **跳过标题/译者等头部**，从第一段连续中文正文开始取签名做相似度比对；
3. 相似度超阈值即判为同书，组内**保留内容最全的那一版**。

```sh
# 查重报告（只读）
python txt_dedupe.py "F:\书库"

# 导出 JSON 报告
python txt_dedupe.py "F:\书库" --json report.json

# 输出去重结果：每组保留最全的一本，其余原样复制
python txt_dedupe.py "F:\书库" --apply --out "F:\书库-去重"

# 调整严格程度（默认 0.55，越小越严格地合并）
python txt_dedupe.py "F:\书库" --threshold 0.7
```

**编码**：自动识别 `UTF-8 / UTF-16 / GB18030 / Big5 / CP1252`（中文小说常见 GBK/GB18030，
而 gb18030 几乎不会解码失败，所以 UTF-8 优先尝试，并用替换字符比例兜底）。

---

## 3. `txt_collect.py` —— 书库 TXT 汇总整理

把一个目录下所有 txt 复制到新目录，并可选清理**孤儿 `.sdr` 阅读进度目录**
（阅读器为每本书生成同名 `.sdr`；书删掉后这些会残留）。

```sh
# 预览：会复制哪些、会删哪些
python txt_collect.py "F:\书库" "F:\书库-汇总"

# 执行（默认平铺到一个目录）
python txt_collect.py "F:\书库" "F:\书库-汇总" --apply

# 保留作者/系列子目录结构
python txt_collect.py "F:\书库" "F:\书库-汇总" --apply --keep-structure

# 同时清理孤儿 .sdr
python txt_collect.py "F:\书库" "F:\书库-汇总" --apply --clean-orphan-sdr
```

**安全设计**：
* 默认只预览，`--apply` 才动手；
* 复制后**逐个校验字节数**；
* 同名文件自动加 `(1)`、`(2)` 后缀（预览阶段就会显示）；
* 孤儿目录删除前对**每个路径**做 4 项断言（是目录 / 以 `.sdr` 结尾 / 严格位于源目录之下 /
  同名 txt 确实不存在），删除前再校验一次；
* **只删除阅读进度元数据，不动书本身**；源目录的书一个都不会动。

---

## 典型流程

```sh
# 1) 先查重，看看有没有同一本书的多个版本
python txt_dedupe.py "F:\[AmberNya]__[书库]\某书库"

# 2) 汇总到一个干净目录（平铺），顺便清掉残留的进度目录
python txt_collect.py "F:\[AmberNya]__[书库]\某书库" "F:\[AmberNya]__[书库]\某书库-汇总" --apply --clean-orphan-sdr

# 3) 打开 NyaView →「转换」→「电子书合集 / TXT」→ 选文件夹 → 合并为一本带目录的 EPUB
```
