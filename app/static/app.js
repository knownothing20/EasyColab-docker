const $ = (id) => document.getElementById(id);

const GPU_RES = [
  { id: "v_832_480", label: "832 × 480 (16:9 横屏 · 推荐)", w: 832, h: 480 },
  { id: "v_480_832", label: "480 × 832 (9:16 竖屏)", w: 480, h: 832 },
  { id: "v_1280_720", label: "1280 × 720 (16:9 720P)", w: 1280, h: 720 },
  { id: "v_720_1280", label: "720 × 1280 (9:16 720P)", w: 720, h: 1280 },
  { id: "v_640_640", label: "640 × 640 (1:1 方形)", w: 640, h: 640 },
];

const IMG_RES = [
  { id: "i_1024_1024", label: "1024 × 1024 (1:1 超清 · 推荐)", w: 1024, h: 1024 },
  { id: "i_1344_768", label: "1344 × 768 (16:9 电影横幅)", w: 1344, h: 768 },
  { id: "i_768_1344", label: "768 × 1344 (9:16 竖版海报)", w: 768, h: 1344 },
  { id: "i_1536_1024", label: "1536 × 1024 (3:2 单反摄影)", w: 1536, h: 1024 },
];

const DURATIONS = [
  { frames: 90, label: "4s" },
  { frames: 124, label: "5s" },
  { frames: 192, label: "8s" },
  { frames: 243, label: "10s" },
  { frames: 294, label: "12s" },
  { frames: 362, label: "15s" },
];

const MODES = [
  { id: "t2va", label: "文生音视频" },
  { id: "fl2va", label: "首尾帧生视频" },
];

const PRESETS = [
  { title: "— 不使用预设 —", visual: "", sound: "" },
  {
    title: "[动漫] 新海诚风 · 盛夏樱花铁道口",
    visual:
      "Masterpiece Makoto Shinkai anime style, 2D cel-shaded animation. A teenage girl in a Japanese sailor school uniform standing at a sunlit railway crossing in late spring. A green commuter train speeds past in front of her, creating a strong gust of wind that blows her long black hair and a flurry of pink cherry blossom petals across the frame. Dramatic lens flare, glowing cumulus clouds in a deep blue sky.",
    sound: "Wind gust, distant train rumble, gentle piano melody, soft stereo ambience.",
  },
  {
    title: "[实拍] 雨后城市霓虹街景",
    visual:
      "Cinematic live-action shot of a rainy neon-lit city street at night. Wet asphalt reflecting magenta and cyan signage, steam rising from vents, blurred pedestrians with umbrellas walking past. Shallow depth of field, anamorphic lens flares, slow dolly-in movement.",
    sound: "Light rain pattering, distant traffic, muffled chatter, low synth drone.",
  },
];

const state = {
  connected: false,
  models: [],
  customModes: [],
  taskJobId: null,
  poller: null,
  modalPoll: null,
};

function toast(msg, ms = 2200) {
  const box = $("toast");
  box.textContent = msg;
  box.classList.add("show");
  clearTimeout(box._t);
  box._t = setTimeout(() => box.classList.remove("show"), ms);
}

async function api(path, opts = {}) {
  const { timeout = 20000, ...rest } = opts;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      signal: ctrl.signal,
      ...rest,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === "AbortError") {
      throw new Error(`请求超时 (${timeout / 1000}s)，后端无响应。通常是容器无法访问 Google，请检查代理设置`);
    }
    throw new Error("网络错误: " + e.message);
  }
  clearTimeout(timer);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function fmt(n, d = 1) {
  const v = Number(n);
  if (!isFinite(v)) return "—";
  return v.toFixed(d).replace(/\.0+$/, "");
}

function setBar(el, used, total) {
  if (!el) return;
  const pct = total > 0 ? Math.min(100, (Number(used) / Number(total)) * 100) : 0;
  el.style.width = pct + "%";
}

function setPill(el, text, cls) {
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

function fillSelect(sel, items, valueKey, labelKey) {
  sel.innerHTML = "";
  for (const it of items) {
    const opt = document.createElement("option");
    opt.value = it[valueKey];
    opt.textContent = it[labelKey];
    sel.appendChild(opt);
  }
}

function initSelects() {
  fillSelect($("resSelect"), GPU_RES, "id", "label");
  fillSelect($("durationSelect"), DURATIONS, "frames", "label");
  fillSelect($("presetSelect"), PRESETS, "title", "title");
  $("durationSelect").value = "124";
}

function initTabs() {
  const tabs = $("modeTabs");
  tabs.innerHTML = "";
  for (const m of MODES) {
    const b = document.createElement("button");
    b.textContent = m.label;
    b.dataset.mode = m.id;
    b.onclick = () => {
      [...tabs.children].forEach((c) => c.classList.remove("on"));
      b.classList.add("on");
    };
    tabs.appendChild(b);
  }
  tabs.children[0].classList.add("on");
}

function currentMode() {
  const on = $("modeTabs").querySelector("button.on");
  return on ? on.dataset.mode : "t2va";
}

async function loadState() {
  try {
    const st = await api("/api/state");
    $("serverUrl").value = st.serverUrl || "";
    if (st.gpu) $("gpuSelect").value = st.gpu;
    if (st.idleMinutes != null) $("idleSelect").value = String(st.idleMinutes);
    if (st.serverUrl) {
      state.connected = true;
      setPill($("connBadge"), "已连接", "ok");
    } else {
      setPill($("connBadge"), "未启动", "");
    }
    $("keepaliveToggle").checked = !!(st.keepalive && st.keepalive.running);
    renderLog(st.wake && st.wake.lines ? st.wake.lines : []);
    if (st.wake && st.wake.running) setPill($("connBadge"), "部署中", "busy");
  } catch (e) {
    toast("读取状态失败: " + e.message);
  }
}

function renderLog(lines) {
  const box = $("logBox");
  const merged = Array.isArray(lines) ? lines.slice(-260) : [];
  box.textContent = merged.join("\n");
  box.scrollTop = box.scrollHeight;
}

async function pollWake() {
  try {
    const d = await api("/api/wake");
    const w = d.wake || {};
    renderLog(w.lines || []);
    if (w.url) {
      $("serverUrl").value = w.url;
      state.connected = true;
      setPill($("connBadge"), "就绪", "ok");
    }
    if (w.running) {
      setPill($("connBadge"), "部署中", "busy");
      $("wakeBtn").disabled = true;
      $("wakeBtn").textContent = "正在唤醒部署…";
    } else {
      $("wakeBtn").disabled = false;
      $("wakeBtn").textContent = "唤醒实例并部署";
      if (w.state === "error") setPill($("connBadge"), "失败", "bad");
    }
  } catch (_) { /* keep polling silent */ }
}

async function doWake() {
  const gpu = $("gpuSelect").value;
  try {
    $("wakeBtn").disabled = true;
    $("wakeBtn").textContent = "正在唤醒部署…";
    await api("/api/wake", { method: "POST", body: JSON.stringify({ gpu, idleMinutes: Number($("idleSelect").value) }) });
    toast("已提交唤醒任务，部署需数分钟");
    pollWake();
  } catch (e) {
    toast("唤醒失败: " + e.message);
    $("wakeBtn").disabled = false;
    $("wakeBtn").textContent = "唤醒实例并部署";
  }
}

async function doConnect() {
  const url = $("serverUrl").value.trim();
  if (!url) return toast("请填写服务地址");
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ server_url: url }) });
    state.connected = true;
    setPill($("connBadge"), "已连接", "ok");
    toast("已保存服务地址");
    refreshStatus();
  } catch (e) {
    toast("保存失败: " + e.message);
  }
}

async function refreshStatus() {
  if (!state.connected) return;
  try {
    const d = await api("/api/cloud/status");
    applyStatus(d);
  } catch (e) {
    if (String(e.message).includes("409")) {
      state.connected = false;
      setPill($("connBadge"), "未启动", "");
    }
  }
}

function applyStatus(d) {
  if (!d) return;
  $("gpuName").textContent = d.gpu_name || (d.cuda_available ? "GPU 已就绪" : "未检测到 GPU");
  $("vramUsed").textContent = fmt(d.vram_used_gb);
  $("vramTotal").textContent = fmt(d.vram_total_gb, 0);
  setBar($("vramBar"), d.vram_used_gb, d.vram_total_gb);
  $("ramUsed").textContent = fmt(d.ram_used_gb);
  $("ramTotal").textContent = fmt(d.ram_total_gb, 0);
  setBar($("ramBar"), d.ram_used_gb, d.ram_total_gb);
  $("diskUsed").textContent = fmt(d.disk_used_gb);
  $("diskTotal").textContent = fmt(d.disk_total_gb, 0);
  setBar($("diskBar"), d.disk_used_gb, d.disk_total_gb);

  $("cuRate").textContent = fmt(d.cu_burn_rate_per_hour, 2) + " CU/h";
  $("cuSession").textContent = fmt(d.session_cu_used, 2) + " CU";
  const up = Number(d.uptime_seconds || 0);
  $("uptime").textContent = up > 0 ? Math.floor(up / 60) + " 分钟" : "—";
  const idle = Number(d.idle_remaining_seconds || 0);
  $("idleLeft").textContent = idle > 0 ? Math.floor(idle / 60) + " 分 " + (idle % 60) + " 秒" : (idle === -1 ? "已关闭" : "—");
  if (d.idle_timeout_minutes != null) $("idleSelect").value = String(d.idle_timeout_minutes);

  $("weightsInfo").textContent = (d.downloaded_count ?? 0) + " / " + (d.total_count ?? 0) + " 个文件 · " + fmt(d.total_size_gb, 1) + " GB";
  const loaded = Array.isArray(d.loaded_models_detail) && d.loaded_models_detail.length
    ? d.loaded_models_detail.map((m) => m.name + (m.vram_gb ? ` (${fmt(m.vram_gb)}G)` : "")).join("、")
    : "无";
  $("loadedModels").textContent = loaded;
  $("apiKeyState").textContent = d.api_key_configured ? "已配置" : "未配置";
  $("apiReqCount").textContent = String(d.api_request_count ?? 0);
  $("taskMessage").textContent = d.task_message || (d.is_downloading ? "下载中…" : "空闲");
  $("taskProgress").style.width = Math.min(100, Number(d.task_progress || 0) * 100) + "%";

  const comfy = d.comfyui || {};
  $("comfyState").textContent = comfy.is_running ? (comfy.is_starting ? "启动中" : "运行中") : "未启动";
  $("comfyQueue").textContent = (comfy.queue_running ?? 0) + " 运行 · " + (comfy.queue_pending ?? 0) + " 排队";
  $("comfyOpenBtn").disabled = !comfy.is_running || !comfy.public_url;

  if (d.console_lines && d.console_lines.length) renderLog(d.console_lines);

  syncModelOptions(d);
  if (Array.isArray(d.custom_models)) state.models = d.custom_models;
}

function syncModelOptions(d) {
  const sel = $("modelSelect");
  const opts = [];
  const seen = new Set();
  for (const m of (state.models || [])) {
    if (!m || !m.id) continue;
    seen.add(m.id);
    opts.push({ id: m.id, name: m.name || m.id });
  }
  if (d.selected_model_id && !seen.has(d.selected_model_id)) {
    opts.unshift({ id: d.selected_model_id, name: d.selected_model_name || d.selected_model_id });
  }
  if (!opts.length) return;
  const prev = sel.value;
  sel.innerHTML = "";
  for (const o of opts) {
    const el = document.createElement("option");
    el.value = o.id;
    el.textContent = o.name;
    sel.appendChild(el);
  }
  if (prev && opts.some((o) => o.id === prev)) sel.value = prev;
  else if (d.selected_model_id) sel.value = d.selected_model_id;
}

async function loadModels() {
  try {
    const d = await api("/api/cloud/status");
    if (Array.isArray(d.custom_models)) {
      state.models = d.custom_models;
      syncModelOptions(d);
    }
  } catch (_) { /* cloud not ready */ }
}

async function doDownload() {
  const model_id = $("modelSelect").value;
  if (!model_id) return toast("请选择模型");
  try {
    toast("已提交下载任务");
    await api("/api/cloud/download_model", { method: "POST", body: JSON.stringify({ model_id }) });
    startGenPoll();
  } catch (e) { toast("下载失败: " + e.message); }
}

async function doLoad() {
  const model_id = $("modelSelect").value;
  if (!model_id) return toast("请选择模型");
  try {
    toast("已提交加载任务");
    await api("/api/cloud/load_model", {
      method: "POST",
      body: JSON.stringify({ model_id, precision_mode: "bf16", offload_mode: "disk" }),
    });
    startGenPoll();
  } catch (e) { toast("加载失败: " + e.message); }
}

async function doUnload() {
  const model_id = $("modelSelect").value;
  try {
    await api("/api/cloud/unload_model", { method: "POST", body: JSON.stringify({ model_id }) });
    toast("已卸载");
    refreshStatus();
  } catch (e) { toast("卸载失败: " + e.message); }
}

async function doGenerate() {
  const visual_prompt = $("promptInput").value.trim();
  if (!visual_prompt) return toast("请填写提示词");
  const res = GPU_RES.find((r) => r.id === $("resSelect").value) || GPU_RES[0];
  const body = {
    model_id: $("modelSelect").value || undefined,
    task_mode: currentMode(),
    visual_prompt,
    negative_prompt: $("negInput").value.trim(),
    soundscape_prompt: $("soundInput").value.trim(),
    width: res.w,
    height: res.h,
    num_frames: Number($("durationSelect").value || 124),
    num_inference_steps: Number($("stepsInput").value || 35),
    seed: Number($("seedInput").value || 42),
    batch_count: Number($("batchInput").value || 1),
    randomize_seed: true,
  };
  try {
    $("generateBtn").disabled = true;
    const d = await api("/api/cloud/generate", { method: "POST", body: JSON.stringify(body) });
    if (d.status === "failed") {
      toast(d.message || "生成失败");
      $("genMessage").textContent = d.message || "";
      $("generateBtn").disabled = false;
      return;
    }
    state.taskJobId = d.job_id;
    $("genMessage").textContent = "任务已提交…";
    startGenPoll();
  } catch (e) {
    toast("提交失败: " + e.message);
    $("generateBtn").disabled = false;
  }
}

async function doCancel() {
  try {
    await api("/api/cloud/cancel", { method: "POST", body: JSON.stringify({}) });
    toast("已发送取消");
  } catch (e) { toast("取消失败: " + e.message); }
}

function startGenPoll() {
  if (state.poller) return;
  state.poller = setInterval(async () => {
    try {
      if (state.taskJobId) {
        const j = await api("/api/cloud/job/" + state.taskJobId);
        const pct = Math.min(100, Number(j.progress || 0) * 100);
        $("genProgress").style.width = pct + "%";
        $("genMessage").textContent = (j.message || j.status) + (j.elapsed ? ` · ${Math.round(j.elapsed)}s` : "");
        if (isFinite(pct)) $("taskProgress").style.width = pct + "%";
        if (j.status === "completed" || j.status === "failed") {
          $("generateBtn").disabled = false;
          state.taskJobId = null;
          refreshOutputs();
          refreshStatus();
        }
      } else {
        refreshStatus();
      }
    } catch (_) { /* ignore transient */ }
  }, 2000);
}

async function refreshOutputs() {
  try {
    const d = await api("/api/local_outputs");
    const box = $("outputs");
    const items = (d.items || []).filter((x) => x.media_type !== "other");
    if (!items.length) {
      box.innerHTML = '<div class="empty">暂无作品</div>';
      return;
    }
    box.innerHTML = "";
    for (const it of items.slice(0, 24)) {
      const el = document.createElement("div");
      el.className = "out-item";
      const thumb = document.createElement("div");
      thumb.className = "out-thumb";
      if (it.media_type === "image") {
        const img = document.createElement("img");
        img.src = "/api/local_file/" + encodeURIComponent(it.rel);
        img.alt = it.name;
        thumb.appendChild(img);
      } else if (it.media_type === "video") {
        const v = document.createElement("video");
        v.src = "/api/local_file/" + encodeURIComponent(it.rel);
        v.muted = true;
        v.preload = "metadata";
        v.style.width = "100%";
        v.style.height = "100%";
        v.style.objectFit = "cover";
        thumb.appendChild(v);
      } else {
        thumb.textContent = "文件";
      }
      const meta = document.createElement("div");
      meta.className = "out-meta";
      const name = document.createElement("span");
      name.textContent = it.name.length > 18 ? it.name.slice(0, 15) + "…" : it.name;
      const link = document.createElement("a");
      link.href = "/api/local_file/" + encodeURIComponent(it.rel);
      link.download = it.name;
      link.textContent = "下载";
      meta.append(name, link);
      el.append(thumb, meta);
      box.appendChild(el);
    }
  } catch (_) { /* ignore */ }
}

async function doComfy(action) {
  try {
    if (action === "start") {
      await api("/api/cloud/comfyui/start", { method: "POST", body: JSON.stringify({ unload_existing: false, force_restart: false }) });
      toast("正在启动 ComfyUI 画布");
    } else if (action === "open") {
      const d = await api("/api/cloud/comfyui/status");
      const url = d && d.comfyui && d.comfyui.public_url;
      if (url) window.open(url, "_blank", "noopener");
      else toast("画布尚未就绪");
    } else if (action === "free") {
      await api("/api/cloud/comfyui/free_vram", { method: "POST", body: JSON.stringify({}) });
      toast("已清理显存缓存");
    } else if (action === "sync") {
      await api("/api/cloud/comfyui/sync_drive", { method: "POST", body: JSON.stringify({}) });
      toast("已触发云盘同步");
    }
    setTimeout(refreshStatus, 1500);
  } catch (e) { toast("操作失败: " + e.message); }
}

async function doReleaseAll() {
  if (!confirm("释放账号下所有 Colab 实例？计费将立即归零。")) return;
  try {
    await api("/api/stop_all", { method: "POST", body: JSON.stringify({}) });
    $("keepaliveToggle").checked = false;
    state.connected = false;
    setPill($("connBadge"), "未启动", "");
    $("serverUrl").value = "";
    toast("已释放全部实例");
  } catch (e) { toast("释放失败: " + e.message); }
}

async function toggleKeepalive() {
  const on = $("keepaliveToggle").checked;
  try {
    await api("/api/keepalive", { method: "POST", body: JSON.stringify({ on }) });
    toast(on ? "保活守护已启动" : "保活守护已停止");
    pollWake();
  } catch (e) {
    toast("保活操作失败: " + e.message);
    $("keepaliveToggle").checked = !on;
  }
}

async function syncApiKey() {
  const key = $("apiKeyInput").value.trim();
  if (!key) return toast("请填写密钥");
  try {
    await api("/api/cloud/config_api_key", { method: "POST", body: JSON.stringify({ api_key: key, initialize_only: false }) });
    toast("密钥已同步至云端网关");
    $("apiKeyInput").value = "";
    refreshStatus();
  } catch (e) { toast("同步失败: " + e.message); }
}

function openAuthModal() {
  $("authModal").classList.remove("hidden");
}

async function genAuthUrl() {
  try {
    const d = await api("/api/auth/url", { method: "POST", body: JSON.stringify({}) });
    const box = $("authUrlBox");
    const actions = $("authUrlActions");
    box.classList.remove("hidden");
    box.innerHTML = "";
    const a = document.createElement("a");
    a.href = d.authUrl;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = d.authUrl;
    box.appendChild(a);
    actions.classList.remove("hidden");
    $("openAuthUrlLink").href = d.authUrl;
    $("copyAuthUrlBtn").onclick = () => copyText(d.authUrl, "授权链接");
    $("authHint").textContent = "提示：优先点「在新标签打开」直接跳转；或点「复制链接」到浏览器地址栏粘贴。";
  } catch (e) {
    $("authHint").textContent = "生成失败: " + e.message;
  }
}

async function submitAuthCode() {
  // 允许粘贴时被引号或多余空白包裹
  let code = $("authCodeInput").value.trim();
  code = code.replace(/^["'`\s]+|["'`\s]+$/g, "");
  if (!code) return toast("请粘贴授权码");
  const btn = $("authCodeBtn");
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "提交中…";
  $("authHint").textContent = "正在向 Google 交换令牌，最长等待 60 秒…";
  try {
    const d = await api("/api/auth/code", { method: "POST", body: JSON.stringify({ code }), timeout: 65000 });
    $("authHint").textContent = "授权成功";
    toast("Google 账号绑定成功");
    setTimeout(() => $("authModal").classList.add("hidden"), 900);
  } catch (e) {
    $("authHint").textContent = "授权失败: " + e.message;
    toast("授权失败: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

function copyText(text, label) {
  if (!text) return toast("没有可复制的内容");
  const done = () => toast((label || "内容") + "已复制");
  const fail = () => toast("复制失败，请手动选择文本");

  // 老浏览器 / HTTP 页面：navigator.clipboard 不存在，先尝试 execCommand
  if (!navigator.clipboard) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) {}
    ta.remove();
    return ok ? done() : fail();
  }

  // HTTPS / localhost：用 clipboard API
  navigator.clipboard.writeText(text).then(done, () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) {}
    ta.remove();
    ok ? done() : fail();
  });
}

function applyPreset() {
  const p = PRESETS[$("presetSelect").selectedIndex] || PRESETS[0];
  if (!p.visual) return;
  $("promptInput").value = p.visual;
  $("soundInput").value = p.sound || "";
}

function bind() {
  $("wakeBtn").onclick = doWake;
  $("connectBtn").onclick = doConnect;
  $("accountBtn").onclick = openAuthModal;
  $("copyUrlBtn").onclick = () => copyText($("serverUrl").value.trim(), "服务地址");
  $("releaseBtn").onclick = doReleaseAll;
  $("keepaliveToggle").onchange = toggleKeepalive;
  $("comfyStartBtn").onclick = () => doComfy("start");
  $("comfyOpenBtn").onclick = () => doComfy("open");
  $("comfyFreeBtn").onclick = () => doComfy("free");
  $("comfySyncBtn").onclick = () => doComfy("sync");
  $("downloadBtn").onclick = doDownload;
  $("loadBtn").onclick = doLoad;
  $("unloadBtn").onclick = doUnload;
  $("generateBtn").onclick = doGenerate;
  $("cancelBtn").onclick = doCancel;
  $("refreshStatusBtn").onclick = refreshStatus;
  $("refreshOutputsBtn").onclick = refreshOutputs;
  $("apiKeyBtn").onclick = syncApiKey;
  $("presetSelect").onchange = applyPreset;
  $("copyLogBtn").onclick = () => copyText($("logBox").textContent, "运行日志");
  $("idleSelect").onchange = async () => {
    const v = Number($("idleSelect").value);
    await api("/api/settings", { method: "POST", body: JSON.stringify({ idle_minutes: v }) });
    try {
      await api("/api/cloud/config_idle", { method: "POST", body: JSON.stringify({ idle_timeout_minutes: v }) });
      toast("闲置超时已更新");
    } catch (_) { toast("本地已保存，云端未连接"); }
  };
  $("authUrlBtn").onclick = genAuthUrl;
  $("authCodeBtn").onclick = submitAuthCode;
  document.querySelectorAll("[data-close]").forEach((b) => {
    b.onclick = () => $("authModal").classList.add("hidden");
  });
  $("authModal").addEventListener("click", (e) => {
    if (e.target === $("authModal")) $("authModal").classList.add("hidden");
  });
}

async function boot() {
  initSelects();
  initTabs();
  bind();
  await loadState();
  if (state.connected) {
    await refreshStatus();
    await loadModels();
  }
  pollWake();
  setInterval(pollWake, 4000);
  setInterval(() => { if (state.connected) refreshStatus(); }, 6000);
  refreshOutputs();
  setInterval(refreshOutputs, 15000);
}

boot();