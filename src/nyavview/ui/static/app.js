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
};

const $ = (id) => document.getElementById(id);
const KIND_LABEL = { download: "下载", convert: "转码", bili: "B站缓存" };

/* ---------------------------------------------------------- tabs */

function switchPanel(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.panel === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchPanel(tab.dataset.panel));
});

/* ---------------------------------------------------------- toast */

let toastTimer = null;
function toast(message, kind = "info") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3600);
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
}

function upsertJob(job) {
  state.jobs.set(job.id, { ...state.jobs.get(job.id), ...job });
}

/* ---------------------------------------------------------- events */

function handleEvent(ev) {
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
  $("dlDirPath").textContent = `当前：${info.downloadDir}`;
  $("cvDirPath").textContent = `当前：${info.convertedDir}`;
  $("biliDirPath").textContent = `当前：${info.convertedDir}`;
  $("runtimeText").textContent = `ffmpeg ${info.ffmpeg.split(/[\\/]/).pop()} · yt-dlp ${info.ytdlp}`;
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

function pickDir(field) {
  return api().pick_output_dir().then((path) => {
    if (path) {
      state[field] = path;
      const el = $(field === "dlDir" ? "dlDirPath" : field === "cvDir" ? "cvDirPath" : "biliDirPath");
      el.textContent = `当前：${path}`;
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

/* -------- download -------- */

function collectDownload() {
  const cookiesSel = $("dlCookies").value;
  let cookieFile = null;
  let cookiesFromBrowser = null;
  if (cookiesSel === "file") {
    cookieFile = $("dlCookieFileValue") ? $("dlCookieFileValue").textContent : null;
  } else if (cookiesSel) {
    cookiesFromBrowser = cookiesSel;
  }
  return {
    destination: state.dlDir,
    audioOnly: $("dlAudioOnly").checked,
    overwrite: $("dlOverwrite").checked,
    cookieFile,
    cookiesFromBrowser,
  };
}

$("downloadBtn").addEventListener("click", () => {
  const url = $("downloadUrl").value.trim();
  if (!url) { toast("请先粘贴下载链接", "error"); return; }
  const options = collectDownload();
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

/* -------- convert -------- */

$("cvStart").addEventListener("click", () => {
  const source = $("cvFilePath").dataset.file;
  if (!source) { toast("请先选择要转换的视频文件", "error"); return; }
  const options = {
    destination: state.cvDir,
    overwrite: $("cvOverwrite").checked,
    preset: $("cvPreset").value,
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

$("settingsSave").addEventListener("click", () => {
  const payload = {
    download_dir: $("setDownloadDir").value.trim() || null,
    converted_dir: $("setConvertedDir").value.trim() || null,
    audio_bitrate: $("setBitrate").value.trim() || null,
    video_preset: $("setPreset").value,
    max_concurrent_tasks: parseInt($("setConcurrent").value, 10) || 2,
    cookies_from_browser: $("setCookies").value || null,
  };
  api().save_settings(payload).then((res) => {
    const msg = $("settingsMsg");
    if (res.ok) {
      msg.textContent = "已保存 ✓";
      msg.className = "settings-msg";
      refreshDirs();
      toast("设置已保存", "success");
    } else {
      msg.textContent = "保存失败：" + (res.error || "未知错误");
      msg.className = "settings-msg error";
    }
  });
});

function loadSettings() {
  api().get_settings().then((s) => {
    if (s.download_dir) $("setDownloadDir").value = s.download_dir;
    if (s.converted_dir) $("setConvertedDir").value = s.converted_dir;
    if (s.audio_bitrate) $("setBitrate").value = s.audio_bitrate;
    if (s.video_preset) $("setPreset").value = s.video_preset;
    if (s.max_concurrent_tasks) $("setConcurrent").value = s.max_concurrent_tasks;
    if (s.cookies_from_browser) $("setCookies").value = s.cookies_from_browser;
  });
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
    $("runtimeText").textContent = "浏览器预览模式（无后端）";
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
