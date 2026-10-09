# NyaView

异步视频下载与转码工具：既有**命令行版本**，也附带 **Claude 风格桌面 UI**（打包为免安装 Windows EXE）。

## 功能

**下载**
- 粘贴链接即下（Bilibili / YouTube / 及 yt-dlp 支持的所有站点）
- 仅音频（可选 **自适应原生 / MP3 / AAC / M4A / FLAC / WAV / OPUS**）
- 多P/分P选择（仅第 1 P / 全部 / 指定范围如 `1-3`、`1,3,5`）
- 预览卡：自动读取标题、封面、时长、可选清晰度
- 画质：默认 **H.264 优先**（所有播放器可放），也可切「最佳（AV1/HEVC）」
- 字幕下载（B站 CC / YouTube）、失败自动重试、自定义命名模板
- Cookie：一键从浏览器导出并**缓存复用**（详见下文 Cookie 说明）

**转换**
- 本地视频 → MP4（H.264 + AAC，兼容老旧播放器；可选画质 CRF）
- Bilibili 客户端缓存 → MP4（`entry.json + video.m4s + audio.m4s`，**递归识别任意层级的缓存文件夹**，多P自动各自成片）
- **图片包 → 电子书**：漫画图片文件夹转 **PDF / CBZ / EPUB**
  - 自动**自然排序**（`2.png` 排在 `10.png` 前，支持中文目录名与多级子文件夹）
  - **手动排序**：缩略图拖拽（鼠标/触摸均可）+ ▲▼ 按钮 + 一键重置
  - 双击缩略图**查看大图**（可切换上一张/下一张）
  - 两种模式：合并成一本书 / 每个子文件夹一本

**桌面 UI**
- 五个视图：下载 / 转换 / 队列 / 日志 / 设置
- 实时进度（下载速度 / ETA / 转码百分比）、任务取消、完成后一键打开输出目录
- 深色模式、拖拽文件/文件夹直接入队
- 内置 **ffmpeg**（imageio-ffmpeg 的 gyan.dev build）与 **yt-dlp**（Python 内核），**无需在系统安装 ffmpeg / yt-dlp**

## 运行（开发环境）

```sh
uv sync
uv run pytest -q          # 测试（79 个）
uv run nyavview --help    # CLI
uv run nyavview-gui       # 桌面 UI
```

## 命令行用法

```sh
nyavview download <url> --destination <folder>
nyavview convert <local-video-file> --destination <folder>
nyavview convert <bilibili-cache-folder> --destination <folder>
```

Cookie 支持：

```sh
nyavview download <url> --cookies-from-browser edge
nyavview download <url> --cookie-file cookies.txt
```

配置：复制 `nyavview.toml.example` 为 `nyavview.toml`（放当前目录）即可作为默认配置；
桌面 UI 的设置保存在 `~/.nyavview.toml`，两者同构。

## 桌面 UI（Windows EXE）

预构建产物位于 `dist/NyaView/`（免安装目录版，双击 `NyaView.exe` 即可）：

```sh
uv run pyinstaller --noconfirm --clean nyavview.spec            # 目录版
uv run pyinstaller --noconfirm --clean nyavview-onefile.spec    # 单文件版
```

> 需要 **WebView2 运行时**（Win10/11 通常已内置；旧系统可在 Microsoft 官网安装 Evergreen Runtime）。
> 界面风格对齐 Claude.ai：奶油底色、陶土色强调、衬线标题、圆角卡片。

## 关于 Cookie（重要）

想要 Bilibili 720P/1080P 等高画质，需要带上登录 Cookie。两种方式：

1. **右上角 🍪 按钮**：一键从浏览器导出 Cookie 并缓存为 `~/.nyavview_cookies.txt`，之后一直复用。
   - 若浏览器正在运行，其 Cookie 数据库被锁定会导出失败，请先**完全关闭浏览器**。
   - 新版 Edge/Chrome（128+）使用 **App-Bound 加密**，普通程序**无法解密**（Chromium 的反窃取设计，关浏览器也没用）。
     此时程序会**自动回退尝试 Firefox**（Firefox 的 Cookie 未加密，可正常读取）。
2. **浏览器插件导出**（最通用）：安装如 *Get cookies.txt LOCALLY*，登录后导出 `cookies.txt`，
   在「设置 → 下载 → Cookie 文件」选择该文件即可。

## 架构

分层：`engines`（yt-dlp / ffmpeg / ebook）/ `core`（settings、errors、events、service）/ `utils` / `cli` / `ui`，
同一套 core 同时驱动 CLI 与桌面 UI。

工程评审与重构记录见 [docs/engineering_review.md](docs/engineering_review.md)。

## 开发

```sh
uv run pytest -q
uv run nyavview --help
uv run python selftest_ui.py   # 打开真实窗口自检 UI 渲染
```

## 许可

[MIT](LICENSE)
