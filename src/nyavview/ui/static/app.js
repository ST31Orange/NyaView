/* NyaView frontend logic — talks to the Python Api bridge. */
"use strict";

const api = () => window.pywebview.api;

/* ---------------------------------------------------------- state */

const state = {
  jobs: new Map(),        // jobId -> job
  logLines: [],
  queueTab: null,
  dlDir: null,
  cvDir: null,
  biliDir: null,
  // image -> ebook
  ebookDir: null,
  ebookGroups: [],        // [{ name, images: [{path,name,...}] }]
  ebookThumbs: new Map(), // path -> data URI
};

const $ = (id) => document.getElementById(id);
const KIND_LABEL = { download: "下载", convert: "转码", bili: "B站缓存", ebook: "电子书" };

/* ---------------------------------------------------------- tabs */

function switchPanel(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.panel === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchPanel(tab.dataset.panel));
});

/* Convert page sub-tabs (local file vs Bilibili cache) */
document.querySelectorAll(".sub-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".sub-tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".sub-panel").forEach((p) => p.classList.toggle("active", p.id === `sub-${tab.dataset.sub}`));
  });
});

/* -------- support / donate modal -------- */

function openSupportModal() {
  $("supportModal").style.display = "flex";
}

function closeSupportModal() {
  $("supportModal").style.display = "none";
}

$("supportBtn").addEventListener("click", openSupportModal);
$("supportClose").addEventListener("click", closeSupportModal);
$("supportModal").addEventListener("click", (e) => {
  if (e.target === $("supportModal")) closeSupportModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("supportModal").style.display === "flex") closeSupportModal();
});

/* -------- load & cache cookies -------- */

$("cookieBtn").addEventListener("click", () => {
  const btn = $("cookieBtn");
  btn.disabled = true;
  toast("正在加载 Cookie…", "info");
  api().load_cookies().then((res) => {
    btn.disabled = false;
    if (res.ok) {
      const skipNote = res.skipped ? `（跳过 ${res.skipped} 个被占用的配置）` : "";
      const switchNote = res.switched ? `（${res.requested} 无法读取，已自动改用 ${res.browser}）` : "";
      toast(`已加载 ${res.count} 个 Cookie${switchNote}，已缓存`, "success", 7000);
      log("info", `Cookie 已从 ${res.browser} 导出 ${res.count} 个${skipNote}${switchNote} → ${res.path}`);
      if (res.switched) {
        log("warn", `提示：${res.requested} 使用了 App-Bound 加密无法读取，已改用 ${res.browser}；请确认已在 ${res.browser} 中登录目标网站。`);
      }
    } else {
      const full = `${res.error || ""} ${res.detail || ""} ${res.hint || ""}`.trim();
      toast(full, "error", 9000);
      log("error", `Cookie 加载失败：${full}`);
    }
  }).catch((err) => {
    btn.disabled = false;
    toast("Cookie 加载失败", "error");
  });
});

/* ---------------------------------------------------------- toast */

let toastTimer = null;
function toast(message, kind = "info", durationMs = 3600) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), durationMs);
}

/* ---------------------------------------------------------- log */

function log(level, message) {
  const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  state.logLines.push({ level, ts, message });
  if (state.logLines.length > 800) state.logLines.shift();
  renderLog();
}

function renderLog() {
  const view = $("logView");
  if (!state.logLines.length) {
    view.innerHTML = '<div class="log-empty">— 暂无日志 —</div>';
    return;
  }
  view.innerHTML = state.logLines
    .map((l) => `<div class="log-line ${l.level}"><span class="t">${l.ts}</span>${escapeHtml(l.message)}</div>`)
    .join("");
  view.scrollTop = view.scrollHeight;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------------------------------------------------- queue render */

function statusLabel(status) {
  return { pending: "等待中", running: "进行中", done: "完成", error: "失败", cancelled: "已取消" }[status] || status;
}

function progressText(job) {
  const bits = [];
  if (job.percent != null) bits.push(`${job.percent.toFixed(1)}%`);
  if (job.speed) bits.push(job.speed);
  if (job.eta != null) bits.push(`剩余 ${job.eta}s`);
  return bits.join(" · ");
}

function renderQueue() {
  const list = $("queueList");
  const empty = $("queueEmpty");
  const jobs = [...state.jobs.values()].sort((a, b) => b.createdAt - a.createdAt);
  $("queueCount").textContent = jobs.filter((j) => j.status === "running" || j.status === "pending").length;

  if (!jobs.length) {
    list.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  list.innerHTML = jobs.map((job) => {
    const pct = job.percent != null ? Math.min(100, job.percent) : 0;
    const running = job.status === "running" || job.status === "pending";
    return `
      <div class="job" data-id="${job.id}">
        <div class="job-top">
          <span class="job-kind ${job.kind}">${KIND_LABEL[job.kind] || job.kind}</span>
          <span class="job-label" title="${escapeHtml(job.label)}">${escapeHtml(job.label)}</span>
          <span class="job-status ${job.status}">${statusLabel(job.status)}</span>
        </div>
        <div class="job-bar"><div class="job-bar-fill" style="width:${pct}%"></div></div>
        <div class="job-detail">${job.detail ? escapeHtml(job.detail) : "&nbsp;"}</div>
        <div class="job-meta">
          <span class="job-metrics">${progressText(job)}</span>
          <span class="job-actions">
            ${running ? `<button class="btn-cancel" data-cancel="${job.id}">取消</button>` : ""}
            ${job.status === "done" && job.output ? `<button class="btn-open" data-open="${escapeHtml(job.output)}">打开文件夹</button>` : ""}
          </span>
        </div>
        ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
        ${job.output ? `<div class="job-output">→ ${escapeHtml(job.output)}</div>` : ""}
      </div>`;
  }).join("");

  list.querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.addEventListener("click", () => api().cancel(btn.dataset.cancel).then((r) => {
      if (!r.ok) toast("取消失败", "error");
    }));
  });

  list.querySelectorAll("[data-open]").forEach((btn) => {
    btn.addEventListener("click", () => {
      api().open_folder(btn.dataset.open);
    });
  });
}

function upsertJob(job) {
  state.jobs.set(job.id, { ...state.jobs.get(job.id), ...job });
}

/* ---------------------------------------------------------- events */

function handleEvent(ev) {
  // File/folder drop (delivered via poll): the absolute path comes from the
  // pywebview DOM drop handler; plain JS drops only carry relative paths.
  if (ev.type === "drop") {
    if (ev.path) {
      if (ev.target === "cv") {
        $("cvFilePath").textContent = ev.path;
        $("cvFilePath").dataset.file = ev.path;
      } else if (ev.target === "bili") {
        $("biliPath").textContent = ev.path;
        $("biliPath").dataset.folder = ev.path;
      } else if (ev.target === "ebook") {
        ebookSetFolder(ev.path);
      }
    }
    renderQueue();
    return;
  }
  // Ensure the job exists: events may arrive before we ever saw this job
  // (e.g. submitted in this session), so create it from the event payload.
  let job = state.jobs.get(ev.jobId);
  if (!job) {
    job = {
      id: ev.jobId,
      kind: ev.kind || "download",
      label: ev.detail || `任务 ${ev.jobId}`,
      status: ev.status || "pending",
      createdAt: Date.now(),
    };
    state.jobs.set(ev.jobId, job);
  }
  if (ev.type === "progress") {
    job.kind = ev.kind || job.kind;
    job.progress = ev.progress;
    job.percent = ev.percent;
    job.speed = ev.speed;
    job.eta = ev.eta;
    if (ev.detail) job.detail = ev.detail;
  } else if (ev.type === "status") {
    job.status = ev.status;
    job.error = ev.error;
    job.output = ev.output;
    if (ev.status === "done") {
      toast("任务完成：" + job.label, "success");
      log("success", `[${job.id}] 完成 → ${ev.output || job.label}`);
    } else if (ev.status === "error") {
      toast("任务失败：" + (ev.error || job.label), "error");
      log("error", `[${job.id}] 失败：${ev.error}`);
    } else if (ev.status === "cancelled") {
      log("warn", `[${job.id}] 已取消`);
    } else if (ev.status === "running") {
      log("info", `[${job.id}] 开始：${job.label}`);
    }
  }
  renderQueue();
}

/* ---------------------------------------------------------- polling */

async function poll() {
  try {
    const events = await api().poll();
    events.forEach(handleEvent);
  } catch (err) {
    // bridge not ready yet — ignore
  }
}

/* ---------------------------------------------------------- dialogs & defaults */

async function refreshDirs() {
  const info = await api().runtime_info();
  state.dlDir = info.downloadDir;
  state.cvDir = info.convertedDir;
  state.biliDir = info.convertedDir;
  state.ebookDir = info.convertedDir;
  $("dlDirPath").textContent = `当前：${info.downloadDir}`;
  $("cvDirPath").textContent = `当前：${info.convertedDir}`;
  $("biliDirPath").textContent = `当前：${info.convertedDir}`;
  $("ebookDirPath").textContent = `当前：${info.convertedDir}`;
  $("footerStats").textContent = `ffmpeg ${info.ffmpeg.split(/[\\/]/).pop()} · yt-dlp ${info.ytdlp}`;
}

/* ---------------------------------------------------------- actions */

function optimisticJob(jobId, kind, label) {
  if (state.jobs.has(jobId)) return;
  state.jobs.set(jobId, {
    id: jobId,
    kind,
    label,
    status: "pending",
    createdAt: Date.now(),
  });
  renderQueue();
}

const DIR_LABELS = { dlDir: "dlDirPath", cvDir: "cvDirPath", biliDir: "biliDirPath", ebookDir: "ebookDirPath" };

function pickDir(field) {
  return api().pick_output_dir().then((path) => {
    if (path) {
      state[field] = path;
      const el = $(DIR_LABELS[field]);
      if (el) el.textContent = `当前：${path}`;
    }
    return path;
  });
}

$("dlDirPick").addEventListener("click", () => pickDir("dlDir"));
$("cvDirPick").addEventListener("click", () => pickDir("cvDir"));
$("biliDirPick").addEventListener("click", () => pickDir("biliDir"));

$("cvPickFile").addEventListener("click", () => {
  api().pick_file().then((path) => {
    if (path) {
      $("cvFilePath").textContent = path;
      $("cvFilePath").dataset.file = path;
    }
  });
});

$("biliPickFolder").addEventListener("click", () => {
  api().pick_folder().then((path) => {
    if (path) {
      $("biliPath").textContent = path;
      $("biliPath").dataset.folder = path;
    }
  });
});

/* -------- image pack -> ebook -------- */

const EBOOK_THUMB_BATCH = 24;
const EBOOK_THUMB_SIZE = 300; // rendered ~150px wide, so 300 keeps it crisp
let dragFrom = null; // { group, index } while a thumbnail is being dragged
let imgModalAt = null; // { group, index } currently shown in the lightbox

function ebookFlatImages() {
  return state.ebookGroups.flatMap((g) => g.images);
}

function ebookSetFolder(folder) {
  $("ebookPath").textContent = folder;
  $("ebookPath").dataset.folder = folder;
  if (!$("ebookTitle").value.trim()) {
    const base = folder.split(/[\\/]/).filter(Boolean).pop() || "";
    $("ebookTitle").placeholder = base || "默认用文件夹名";
  }
  scanEbook(folder);
}

async function scanEbook(folder) {
  const mode = $("ebookMode").value;
  const res = await api().scan_images(folder, mode);
  if (!res || !res.ok) {
    toast((res && res.error) || "扫描图片失败", "error");
    return;
  }
  state.ebookGroups = (res.groups || []).map((g) => ({
    name: g.name,
    images: (g.images || []).slice(),
  }));
  state.ebookThumbs = new Map();
  $("ebookListWrap").style.display = "block";
  $("ebookRescan").style.display = "";
  renderEbookList();
  loadEbookThumbs();
  toast(`已识别 ${res.total} 张图片（自动排序）`, "success");
  log("info", `[电子书] 扫描 ${folder}：${res.total} 张，${state.ebookGroups.length} 组`);
}

function ebookItemHtml(image, groupIndex, indexInGroup, groupLength) {
  const safeName = escapeHtml(image.name);
  const safePath = escapeHtml(image.path);
  const thumb = state.ebookThumbs.get(image.path);
  const media = thumb
    ? `<img src="${thumb}" alt="" loading="lazy" draggable="false" />`
    : '<div class="ebook-thumb-fallback">🖼</div>';
  return `
    <div class="ebook-item"
         data-group="${groupIndex}" data-index="${indexInGroup}" data-path="${safePath}"
         title="${safePath}  （双击看大图）">
      <span class="ebook-no">${indexInGroup + 1}</span>
      <span class="ebook-grip" data-grip="1" title="按住拖动排序">⠿</span>
      ${media}
      <div class="ebook-name">${safeName}</div>
      <div class="ebook-arrows">
        <button data-move="up" ${indexInGroup === 0 ? "disabled" : ""}>▲</button>
        <button data-move="down" ${indexInGroup === groupLength - 1 ? "disabled" : ""}>▼</button>
      </div>
    </div>`;
}

function renderEbookList() {
  const host = $("ebookList");
  const multi = state.ebookGroups.length > 1;
  host.innerHTML = state.ebookGroups
    .map((group, gi) => {
      const title = multi
        ? `<div class="ebook-group-title">${escapeHtml(group.name || `第 ${gi + 1} 组`)} · ${group.images.length} 张</div>`
        : "";
      const items = group.images
        .map((img, ii) => ebookItemHtml(img, gi, ii, group.images.length))
        .join("");
      return title + items;
    })
    .join("");
  const total = ebookFlatImages().length;
  $("ebookCount").textContent = multi
    ? `已识别 ${total} 张 / ${state.ebookGroups.length} 组`
    : `已识别 ${total} 张`;

  host.querySelectorAll(".ebook-item").forEach((el) => {
    const gi = Number(el.dataset.group);
    const ii = Number(el.dataset.index);
    el.querySelectorAll("[data-move]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        moveEbookImage(gi, ii, btn.dataset.move === "up" ? -1 : 1);
      });
    });
    // Pointer-based reordering: works with mouse, touch and pen, and starts
    // as soon as the pointer moves a few pixels — native HTML5 drag needs a
    // ~500ms long-press on touch (and often fails inside a scrollable list).
    el.addEventListener("pointerdown", (e) => startEbookDrag(e, gi, ii, el));
    // Double-click opens the large single-image view.
    el.addEventListener("dblclick", (e) => {
      e.preventDefault();
      openImageModal(gi, ii);
    });
  });
}

function clearDropMarks() {
  $("ebookList")
    .querySelectorAll(".ebook-item")
    .forEach((n) => n.classList.remove("drop-before", "drop-after"));
}

/** Nearest thumbnail to the pointer, so drops in gaps/empty space still land. */
function ebookDropTarget(x, y) {
  const items = Array.from($("ebookList").querySelectorAll(".ebook-item"));
  let best = null;
  let bestDist = Infinity;
  items.forEach((el) => {
    const r = el.getBoundingClientRect();
    const dx = x - (r.left + r.width / 2);
    const dy = y - (r.top + r.height / 2);
    const d = dx * dx + dy * dy;
    if (d < bestDist) {
      bestDist = d;
      best = { el, rect: r };
    }
  });
  if (!best) return null;
  const after = x > best.rect.left + best.rect.width / 2;
  return {
    el: best.el,
    group: Number(best.el.dataset.group),
    index: Number(best.el.dataset.index),
    after,
  };
}

const EBOOK_DRAG_THRESHOLD = 4; // px before a press becomes a drag

function startEbookDrag(e, group, index, el) {
  if (e.button !== 0 && e.pointerType === "mouse") return; // left button only
  const fromGrip = !!e.target.closest(".ebook-grip");
  // Touch/pen must start on the grip so the list stays scrollable by finger;
  // with a mouse the whole thumbnail is grabbable.
  if (e.pointerType !== "mouse" && !fromGrip) return;
  dragFrom = {
    group,
    index,
    startX: e.clientX,
    startY: e.clientY,
    active: false,
    pointerId: e.pointerId,
    el,
  };
  try {
    el.setPointerCapture(e.pointerId);
  } catch (err) {
    /* capture is best-effort */
  }
}

function moveEbookDrag(e) {
  if (!dragFrom || e.pointerId !== dragFrom.pointerId) return;
  const dx = e.clientX - dragFrom.startX;
  const dy = e.clientY - dragFrom.startY;
  if (!dragFrom.active) {
    if (dx * dx + dy * dy < EBOOK_DRAG_THRESHOLD * EBOOK_DRAG_THRESHOLD) return;
    dragFrom.active = true;
    dragFrom.el.classList.add("dragging");
  }
  e.preventDefault();
  const t = ebookDropTarget(e.clientX, e.clientY);
  clearDropMarks();
  if (t && t.group === dragFrom.group) t.el.classList.add(t.after ? "drop-after" : "drop-before");
  ebookAutoScroll(e.clientY);
}

function endEbookDrag(e) {
  if (!dragFrom || e.pointerId !== dragFrom.pointerId) return;
  const st = dragFrom;
  dragFrom = null;
  try {
    if (st.el.hasPointerCapture && st.el.hasPointerCapture(st.pointerId)) {
      st.el.releasePointerCapture(st.pointerId);
    }
  } catch (err) {
    /* ignore */
  }
  st.el.classList.remove("dragging");
  clearDropMarks();
  if (!st.active) return; // it was a click, not a drag
  const t = ebookDropTarget(e.clientX, e.clientY);
  if (!t || t.group !== st.group) return;
  // Insertion point in the pre-move list: before the target, or after it.
  const insertAt = t.index + (t.after ? 1 : 0);
  // Dropping onto its own slot (or right after itself) changes nothing.
  if (insertAt === st.index || insertAt === st.index + 1) return;
  reorderEbookTo(st.group, st.index, insertAt);
}

/** Keep the list scrolling when dragging near its top/bottom edge. */
function ebookAutoScroll(clientY) {
  const host = $("ebookList");
  const r = host.getBoundingClientRect();
  const margin = 44;
  if (clientY < r.top + margin) host.scrollTop -= 14;
  else if (clientY > r.bottom - margin) host.scrollTop += 14;
}

document.addEventListener("pointermove", moveEbookDrag);
document.addEventListener("pointerup", endEbookDrag);
document.addEventListener("pointercancel", endEbookDrag);

function moveEbookImage(groupIndex, index, delta) {
  const images = state.ebookGroups[groupIndex].images;
  const target = index + delta;
  if (target < 0 || target >= images.length) return;
  // Adjacent move: a plain swap is exactly "shift by one".
  [images[index], images[target]] = [images[target], images[index]];
  renderEbookList();
}

/**
 * Move one page to an insertion point.
 *
 * ``insertAt`` is a position in the list *before* the move (0..length), which
 * is what the drop indicator shows. Removing the page first shifts every later
 * index left by one, so the insertion index must be decremented when moving
 * forwards — otherwise the page lands one slot too far right.
 */
function reorderEbookTo(groupIndex, from, insertAt) {
  const images = state.ebookGroups[groupIndex].images;
  const clamped = Math.max(0, Math.min(insertAt, images.length));
  const [moved] = images.splice(from, 1);
  const finalAt = clamped > from ? clamped - 1 : clamped;
  images.splice(Math.max(0, Math.min(finalAt, images.length)), 0, moved);
  renderEbookList();
}

function loadEbookThumbs() {
  const pending = ebookFlatImages()
    .map((img) => img.path)
    .filter((p) => !state.ebookThumbs.has(p));
  if (!pending.length) return;
  const batch = pending.slice(0, EBOOK_THUMB_BATCH);
  api().ebook_thumbnails(batch, EBOOK_THUMB_SIZE).then((res) => {
    if (!res || !res.ok) return;
    (res.thumbs || []).forEach((t) => {
      if (t.data) state.ebookThumbs.set(t.path, t.data);
    });
    renderEbookList();
    if (pending.length > batch.length) loadEbookThumbs();
  });
}

$("ebookPickFolder").addEventListener("click", () => {
  api().pick_folder().then((path) => {
    if (path) ebookSetFolder(path);
  });
});

/* -------- large single-image view (double-click a thumbnail) -------- */

function openImageModal(groupIndex, index) {
  const group = state.ebookGroups[groupIndex];
  if (!group || !group.images[index]) return;
  imgModalAt = { group: groupIndex, index };
  $("imgModal").style.display = "flex";
  loadImageModal();
}

function closeImageModal() {
  $("imgModal").style.display = "none";
  imgModalAt = null;
  $("imgModalImg").removeAttribute("src");
}

function loadImageModal() {
  if (!imgModalAt) return;
  const { group, index } = imgModalAt;
  const image = state.ebookGroups[group] && state.ebookGroups[group].images[index];
  if (!image) return;
  $("imgModalTitle").textContent = `${index + 1} / ${state.ebookGroups[group].images.length} · ${image.name}`;
  $("imgModalMeta").textContent = image.width
    ? `${image.width}×${image.height}`
    : "";
  $("imgModalLoading").style.display = "block";
  $("imgModalImg").removeAttribute("src");
  api().ebook_preview(image.path, 1800).then((res) => {
    if (!imgModalAt || imgModalAt.group !== group || imgModalAt.index !== index) return;
    $("imgModalLoading").style.display = "none";
    if (res && res.ok) {
      $("imgModalImg").src = res.data;
      $("imgModalMeta").textContent =
        `${res.width}×${res.height}` + (res.shownWidth < res.width ? `（预览 ${res.shownWidth}×${res.shownHeight}）` : "");
    } else {
      toast((res && res.error) || "无法加载图片", "error");
    }
  });
}

function stepImageModal(delta) {
  if (!imgModalAt) return;
  const images = state.ebookGroups[imgModalAt.group].images;
  const next = imgModalAt.index + delta;
  if (next < 0 || next >= images.length) return;
  imgModalAt.index = next;
  loadImageModal();
}

$("imgModalClose").addEventListener("click", closeImageModal);
$("imgModalPrev").addEventListener("click", () => stepImageModal(-1));
$("imgModalNext").addEventListener("click", () => stepImageModal(1));
$("imgModal").addEventListener("click", (e) => {
  if (e.target === $("imgModal") || e.target.classList.contains("lightbox-body")) closeImageModal();
});
document.addEventListener("keydown", (e) => {
  if ($("imgModal").style.display !== "flex") return;
  if (e.key === "Escape") closeImageModal();
  else if (e.key === "ArrowLeft") stepImageModal(-1);
  else if (e.key === "ArrowRight") stepImageModal(1);
});

$("ebookRescan").addEventListener("click", () => {
  const folder = $("ebookPath").dataset.folder;
  if (folder) scanEbook(folder);
});

$("ebookMode").addEventListener("change", () => {
  const folder = $("ebookPath").dataset.folder;
  if (folder) scanEbook(folder); // regroup
});

$("ebookReset").addEventListener("click", () => {
  const folder = $("ebookPath").dataset.folder;
  if (folder) scanEbook(folder);
});

$("ebookDirPick").addEventListener("click", () => pickDir("ebookDir"));

$("ebookStart").addEventListener("click", () => {
  const folder = $("ebookPath").dataset.folder;
  if (!folder) { toast("请先选择图片文件夹", "error"); return; }
  const images = ebookFlatImages();
  if (!images.length) { toast("没有可转换的图片", "error"); return; }
  const options = {
    destination: state.ebookDir,
    format: $("ebookFormat").value,
    mode: $("ebookMode").value,
    title: $("ebookTitle").value.trim() || undefined,
    author: $("ebookAuthor").value.trim() || undefined,
    overwrite: $("ebookOverwrite").checked,
    orders: state.ebookGroups
      .filter((g) => g.images.length)
      .map((g) => ({ name: g.name, images: g.images.map((i) => i.path) })),
  };
  api().start_ebook(folder, options).then((res) => {
    if (res.ok) {
      log("info", `[${res.jobId}] 提交电子书生成：${folder}（${images.length} 张，${options.format.toUpperCase()}）`);
      optimisticJob(res.jobId, "ebook", $("ebookTitle").value.trim() || folder.split(/[\\/]/).filter(Boolean).pop());
      switchPanel("queue");
    } else {
      toast(res.error || "提交失败", "error");
    }
  });
});

/* -------- download -------- */

function collectPlaylistItems() {  const mode = $("dlParts").value;
  if (mode === "all") return "1-9999";
  if (mode === "range") {
    const range = ($("dlPartsRange").value || "").trim();
    if (!range) { toast("请输入分P范围，例如 1-3 或 1,3,5", "error"); return null; }
    return range;
  }
  return null; // single P
}

function collectDownload() {
  const cookiesSel = $("dlCookies").value;
  let cookieFile = null;
  let cookiesFromBrowser = null;
  if (cookiesSel === "file") {
    cookieFile = state.cookieFile || null;
  } else if (cookiesSel) {
    cookiesFromBrowser = cookiesSel;
  }
  const items = collectPlaylistItems();
  if (items === null && $("dlParts").value === "range") return null;
  const formatId = state.pickedFormat || null;
  const audioOnly = $("dlAudioOnly").checked;
  return {
    destination: state.dlDir,
    audioOnly,
    overwrite: $("dlOverwrite").checked,
    cookieFile,
    cookiesFromBrowser,
    playlistItems: items,
    preferH264: $("dlQuality").value === "h264",
    writeSubs: $("dlWriteSubs").checked,
    maxRetries: $("dlAutoRetry").checked ? 3 : 1,
    outtmpl: ($("dlNaming").value || "").trim() || undefined,
    format: audioOnly ? null : formatId,
    audioFormat: audioOnly ? $("dlAudioFormat").value : null,
  };
}

$("downloadBtn").addEventListener("click", () => {
  const url = $("downloadUrl").value.trim();
  if (!url) { toast("请先粘贴下载链接", "error"); return; }
  const options = collectDownload();
  if (!options) return;
  api().start_download(url, options).then((res) => {
    if (res.ok) {
      log("info", `[${res.jobId}] 提交下载：${url}`);
      optimisticJob(res.jobId, "download", url);
      switchPanel("queue");
    } else {
      toast(res.error || "提交失败", "error");
    }
  });
});

$("downloadUrl").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("downloadBtn").click();
});

/* -------- preview (E/C): debounce extraction of title/thumbnail/formats */
let previewTimer = null;
function requestPreview() {
  clearTimeout(previewTimer);
  const url = $("downloadUrl").value.trim();
  if (!url) { $("dlPreview").style.display = "none"; return; }
  previewTimer = setTimeout(() => {
    api().preview(url, $("dlQuality").value === "h264").then((res) => {
      if (!res || !res.ok || !res.title) { $("dlPreview").style.display = "none"; return; }
      $("dlPreviewTitle").textContent = res.title;
      const metaBits = [];
      if (res.uploader) metaBits.push(res.uploader);
      if (res.duration) metaBits.push(`${Math.floor(res.duration / 60)}:${String(Math.floor(res.duration % 60)).padStart(2, "0")}`);
      if (res.playlistCount) metaBits.push(`${res.playlistCount} P`);
      $("dlPreviewMeta").textContent = metaBits.join(" · ");
      const thumb = $("dlPreviewThumb");
      if (res.thumbnail) { thumb.src = res.thumbnail; thumb.style.display = ""; }
      else { thumb.style.display = "none"; }
      // format picker (C)
      const sel = $("dlFormat");
      sel.innerHTML = "";
      const opts = res.formats || [];
      if (opts.length) {
        opts.forEach((f) => {
          const o = document.createElement("option");
          o.value = f.id;
          const vc = String(f.vcodec || "").toLowerCase();
          let codec;
          if (vc.startsWith("av01")) codec = "AV1";
          else if (vc.startsWith("avc")) codec = "H.264";
          else if (vc.startsWith("hev")) codec = "HEVC";
          else codec = (f.vcodec || "?").toUpperCase();
          o.textContent = `${f.height ? f.height + "P" : "?"} ${f.fps || ""} · ${codec}`;
          sel.appendChild(o);
        });
        sel.value = opts[0].id;
        state.pickedFormat = opts[0].id;
        sel.onchange = () => { state.pickedFormat = sel.value; };
      } else {
        state.pickedFormat = null;
      }
      $("dlPreview").style.display = "flex";
    }).catch(() => { $("dlPreview").style.display = "none"; });
  }, 900);
}
$("downloadUrl").addEventListener("input", requestPreview);
// switching 画质 re-filters the preview format list (H.264 vs AV1/HEVC)
$("dlQuality").addEventListener("change", requestPreview);

$("dlPreviewClose").addEventListener("click", () => {
  $("dlPreview").style.display = "none";
  state.pickedFormat = null;
});

/* multi-P select: show the range input when '指定范围' is chosen */
$("dlParts").addEventListener("change", () => {
  $("dlPartsRange").style.display = $("dlParts").value === "range" ? "" : "none";
});

/* 仅音频: swap 画质 for 音频格式 (video quality is irrelevant for audio) */
function syncAudioOnlyUI() {
  const on = $("dlAudioOnly").checked;
  $("dlAudioFormat").style.display = on ? "" : "none";
  $("dlAudioFormatLabel").style.display = on ? "" : "none";
  // hide the 画质 select + its label when audio-only
  const qLabel = $("dlQuality").previousElementSibling;
  $("dlQuality").style.display = on ? "none" : "";
  if (qLabel && qLabel.classList.contains("field-label") && qLabel.textContent.trim() === "画质") {
    qLabel.style.display = on ? "none" : "";
  }
}
$("dlAudioOnly").addEventListener("change", syncAudioOnlyUI);

/* cookie file: show the picker when '使用 Cookie 文件' is chosen */
$("dlCookies").addEventListener("change", () => {
  $("dlCookiePick").style.display = $("dlCookies").value === "file" ? "" : "none";
});

$("dlCookiePick").addEventListener("click", () => {
  api().pick_file("选择 Cookie 文件").then((path) => {
    if (path) {
      state.cookieFile = path;
      $("dlCookiePath").textContent = `Cookie 文件：${path}`;
    }
  });
});

/* -------- convert -------- */

$("cvStart").addEventListener("click", () => {
  const source = $("cvFilePath").dataset.file;
  if (!source) { toast("请先选择要转换的视频文件", "error"); return; }
  const options = {
    destination: state.cvDir,
    overwrite: $("cvOverwrite").checked,
    preset: $("cvPreset").value,
    videoCrf: parseInt($("cvQuality").value, 10),
    audioBitrate: $("cvBitrate").value,
  };
  api().start_convert(source, options).then((res) => {
    if (res.ok) {
      log("info", `[${res.jobId}] 提交转码：${source}`);
      optimisticJob(res.jobId, "convert", source.split(/[\\/]/).pop());
      switchPanel("queue");
    } else {
      toast(res.error || "提交失败", "error");
    }
  });
});

$("biliStart").addEventListener("click", () => {
  const source = $("biliPath").dataset.folder;
  if (!source) { toast("请先选择 Bilibili 缓存文件夹", "error"); return; }
  const options = {
    destination: state.biliDir,
    overwrite: $("biliOverwrite").checked,
    maxConcurrentTasks: parseInt($("biliConcurrent").value, 10),
  };
  api().start_bili(source, options).then((res) => {
    if (res.ok) {
      log("info", `[${res.jobId}] 提交 B站缓存转换：${source}`);
      optimisticJob(res.jobId, "bili", source.split(/[\\/]/).pop());
      switchPanel("queue");
    } else {
      toast(res.error || "提交失败", "error");
    }
  });
});

/* -------- queue / log -------- */

$("clearFinished").addEventListener("click", () => {
  api().clear_finished().then(() => {
    [...state.jobs.keys()].forEach((id) => {
      const j = state.jobs.get(id);
      if (["done", "error", "cancelled"].includes(j.status)) state.jobs.delete(id);
    });
    renderQueue();
  });
});

$("clearLog").addEventListener("click", () => {
  state.logLines = [];
  renderLog();
});

/* -------- settings -------- */

$("setCookieFilePick").addEventListener("click", () => {
  api().pick_file("选择 Cookie 文件（cookies.txt）").then((path) => {
    if (path) {
      $("setCookieFile").value = path;
      toast("已选择 Cookie 文件，记得点「保存设置」", "info");
    }
  });
});

$("settingsSave").addEventListener("click", () => {
  const payload = {
    download_dir: $("setDownloadDir").value.trim() || null,
    converted_dir: $("setConvertedDir").value.trim() || null,
    audio_bitrate: $("setBitrate").value.trim() || null,
    video_preset: $("setPreset").value,
    video_crf: parseInt($("setCrf").value, 10) || 23,
    max_concurrent_tasks: parseInt($("setConcurrent").value, 10) || 2,
    cookies_from_browser: $("setCookies").value || null,
    cookie_file: $("setCookieFile").value.trim() || null,
    naming_template: $("setNaming").value.trim() || null,
    dark_mode: $("setDark").value === "true",
  };
  api().save_settings(payload).then((res) => {
    const msg = $("settingsMsg");
    if (res.ok) {
      msg.textContent = "已保存 ✓";
      msg.className = "settings-msg";
      refreshDirs();
      applyDark(payload.dark_mode);
      toast("设置已保存", "success");
    } else {
      msg.textContent = "保存失败：" + (res.error || "未知错误");
      msg.className = "settings-msg error";
    }
  });
});

function applyDark(enabled) {
  document.body.classList.toggle("dark", !!enabled);
}

function loadSettings() {
  api().get_settings().then((s) => {
    if (s.download_dir) $("setDownloadDir").value = s.download_dir;
    if (s.converted_dir) $("setConvertedDir").value = s.converted_dir;
    if (s.audio_bitrate) $("setBitrate").value = s.audio_bitrate;
    if (s.video_preset) $("setPreset").value = s.video_preset;
    if (s.video_crf) $("setCrf").value = s.video_crf;
    if (s.max_concurrent_tasks) $("setConcurrent").value = s.max_concurrent_tasks;
    if (s.cookies_from_browser) $("setCookies").value = s.cookies_from_browser;
    if (s.cookie_file) $("setCookieFile").value = s.cookie_file;
    if (s.naming_template) { $("setNaming").value = s.naming_template; $("dlNaming").value = s.naming_template; }
    if (s.dark_mode !== undefined) { $("setDark").value = String(!!s.dark_mode); applyDark(!!s.dark_mode); }
  });
}

/* -------- drag & drop (B): drop files / folders onto the convert cards
   NOTE: the absolute path arrives through api.poll() as a {type:"drop"}
   event (set by the Python-side pywebview DOM drop handler). The plain JS
   drop below only manages the visual state and an optimistic path when the
   File happens to carry an absolute one. */

function setupDropZone(zoneId) {
  const zone = $(zoneId);
  if (!zone) return;
  ["dragenter", "dragover"].forEach((evt) =>
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((evt) =>
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove("dragover"); })
  );
  zone.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files ? Array.from(e.dataTransfer.files) : [];
    if (files.length) {
      const p = files[0].pywebviewFullPath || files[0].path;
      if (p && /^[A-Za-z]:[\\/]/.test(p)) {
        if (zone.id === "cvDropZone") {
          $("cvFilePath").textContent = p;
          $("cvFilePath").dataset.file = p;
        } else if (zone.id === "ebookDropZone") {
          ebookSetFolder(p);
        } else {
          $("biliPath").textContent = p;
          $("biliPath").dataset.folder = p;
        }
      }
    }
  });
}

setupDropZone("cvDropZone");
setupDropZone("biliDropZone");
setupDropZone("ebookDropZone");

/* -------- update check (J) -------- */

function checkUpdate() {
  // Lightweight check against a static version manifest (no auto-install).
  try {
    fetch("https://raw.githubusercontent.com/nya-view/nyaview/main/version.json", { mode: "cors" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && data.version && data.version !== "0.1.0") {
          log("info", `发现新版本 ${data.version}，请到发布页更新`);
          toast(`发现新版本 ${data.version}`, "info");
        }
      })
      .catch(() => { /* offline — ignore */ });
  } catch (err) { /* ignore */ }
}

/* ---------------------------------------------------------- boot */

function boot() {
  log("info", "NyaView 已启动");
  refreshDirs();
  loadSettings();
  // restore live jobs
  api().jobs().then((jobs) => {
    jobs.forEach(upsertJob);
    renderQueue();
  });
  setInterval(poll, 150);
  checkUpdate();
}

window.addEventListener("pywebviewready", boot);
// Fallback for dev-in-browser preview (no pywebview). Wait a short grace
// period: at DOMContentLoaded pywebview may not have injected the bridge yet
// (it does so after the page finishes loading), so check again before giving
// up — otherwise the preview-mode message races with a real boot().
if (!window.pywebview) {
  let tries = 0;
  const probe = () => {
    if (window.pywebview) return; // bridge arrived — boot() will handle it
    if (++tries < 8) { setTimeout(probe, 250); return; }
    $("footerStats").textContent = "浏览器预览模式（无后端）";
    log("warn", "未检测到 pywebview 桥接，功能不可用（浏览器预览模式）");
    renderQueue();
  };
  document.addEventListener("DOMContentLoaded", probe);
}

// Self-test hook (dev only): with ?selftest=1 the page reports DOM stats back
// through the bridge. Uses the JS->Python direction only, so it never deadlocks
// the bridge (which evaluate_js would, because app.js polls the bridge).
if (new URLSearchParams(location.search).get("selftest") === "1") {
  window.addEventListener("pywebviewready", () => {
    setTimeout(() => {
      try {
        api().selftest_report({
          title: document.title,
          cards: document.querySelectorAll(".card").length,
          tabs: document.querySelectorAll(".tab").length,
          urlInput: !!document.getElementById("downloadUrl"),
          brand: (document.querySelector(".brand-name") || {}).textContent || null,
          queue: state.jobs.size,
        });
      } catch (err) {
        log("error", "selftest report failed: " + err);
      }
    }, 1200);
  });
}
