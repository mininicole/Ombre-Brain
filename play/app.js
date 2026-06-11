const HISTORY_KEY = "tag-dice-history-v2";
const STYLE_KEY = "tag-dice-style";
const PRESET_KEY = "tag-dice-preset";
const HISTORY_MAX = 8;

const state = {
  library: null,
  styleKey: null,
  presetKey: null,
  current: {},
  history: []
};

const $ = (sel) => document.querySelector(sel);

// ----------------- utils -----------------
function pick(arr, exclude) {
  const pool = arr.filter(t => !isBlocked(t) && t !== exclude);
  if (!pool.length) return arr.find(t => !isBlocked(t)) || null;
  return pool[Math.floor(Math.random() * pool.length)];
}

function isBlocked(tag) {
  const list = state.library.blocklist || [];
  const lower = String(tag).toLowerCase();
  return list.some(b => lower.includes(String(b).toLowerCase()));
}

function poolFor(dim, presetKey) {
  const preset = state.library.presets.find(p => p.key === presetKey);
  const poolKey = (preset && preset.use[dim.key]) || "default";
  return dim.pools[poolKey] || dim.pools.default;
}

function activeDims() {
  const preset = state.library.presets.find(p => p.key === state.presetKey);
  if (!preset) return state.library.dimensions;
  return state.library.dimensions.filter(d => preset.use[d.key]);
}

function presetLabel(styleKey, presetKey) {
  const lib = window.TAG_LIBRARY[styleKey];
  if (!lib) return presetKey;
  const p = lib.presets.find(x => x.key === presetKey);
  return p ? p.label : presetKey;
}

function styleLabel(styleKey) {
  const lib = window.TAG_LIBRARY[styleKey];
  return lib ? lib.name : styleKey;
}

function relativeTime(ts) {
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(ts).toLocaleDateString("zh-CN");
}

async function clipboardCopy(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// ----------------- actions -----------------
function rollAll(skipHistory) {
  state.current = {};
  for (const dim of activeDims()) {
    state.current[dim.key] = pick(poolFor(dim, state.presetKey));
  }
  triggerRollAnim();
  if (!skipHistory) pushHistory();
  render();
}

function rerollOne(dimKey) {
  const dim = state.library.dimensions.find(d => d.key === dimKey);
  const prev = state.current[dimKey];
  state.current[dimKey] = pick(poolFor(dim, state.presetKey), prev);
  render(dimKey);
}

function triggerRollAnim() {
  const btn = $("#roll");
  if (!btn) return;
  btn.classList.remove("rolling");
  // 强制 reflow 让动画能重放
  void btn.offsetWidth;
  btn.classList.add("rolling");
  setTimeout(() => btn.classList.remove("rolling"), 600);
}

function copyAllText() {
  return activeDims()
    .map(d => state.current[d.key])
    .filter(Boolean)
    .join(" / ");
}

function pushHistory() {
  const items = activeDims().map(d => ({
    key: d.key, label: d.label, tag: state.current[d.key]
  })).filter(x => x.tag);
  if (!items.length) return;
  state.history.unshift({
    style: state.styleKey, preset: state.presetKey,
    items, at: Date.now()
  });
  state.history = state.history.slice(0, HISTORY_MAX);
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(state.history)); } catch {}
  renderHistory();
}

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (raw) state.history = JSON.parse(raw) || [];
  } catch { state.history = []; }
}

function clearHistory() {
  state.history = [];
  try { localStorage.removeItem(HISTORY_KEY); } catch {}
  renderHistory();
}

// ----------------- render -----------------
function render(flashDim) {
  const card = $("#card");
  const ul = $("#result");
  const chipsBox = $("#chips");
  const dims = activeDims();
  if (!dims.length || !Object.keys(state.current).length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  // 强制重放 stagger 动画
  ul.classList.remove("animate-in");
  chipsBox.classList.remove("animate-in");
  void ul.offsetWidth;

  ul.innerHTML = "";
  for (const dim of dims) {
    const tag = state.current[dim.key];
    if (!tag) continue;
    const li = document.createElement("li");
    if (dim.key === flashDim) li.classList.add("flash");
    li.innerHTML = `
      <span class="dim"></span>
      <span class="tag"></span>
      <button class="reroll" data-dim="${dim.key}" title="重抽这一条" aria-label="重抽">↻</button>
    `;
    li.querySelector(".dim").textContent = dim.label;
    li.querySelector(".tag").textContent = tag;
    ul.appendChild(li);
  }

  chipsBox.innerHTML = "";
  for (const dim of dims) {
    const tag = state.current[dim.key];
    if (!tag) continue;
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.type = "button";
    chip.dataset.tag = tag;
    chip.title = `点击复制 "${tag}"`;
    chip.textContent = tag;
    chipsBox.appendChild(chip);
  }

  ul.classList.add("animate-in");
  chipsBox.classList.add("animate-in");
}

function renderStyleSwitch() {
  const wrap = $("#styleSwitch");
  wrap.innerHTML = "";
  for (const key of Object.keys(window.TAG_LIBRARY)) {
    const lib = window.TAG_LIBRARY[key];
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "style-btn" + (key === state.styleKey ? " active" : "");
    btn.textContent = lib.name;
    btn.dataset.style = key;
    wrap.appendChild(btn);
  }
}

function renderPresets() {
  const wrap = $("#presets");
  wrap.innerHTML = "";
  for (const preset of state.library.presets) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "preset-btn" + (preset.key === state.presetKey ? " active" : "");
    btn.textContent = preset.label;
    btn.dataset.preset = preset.key;
    wrap.appendChild(btn);
  }
}

function renderHistory() {
  const wrap = $("#historySection");
  const list = $("#historyList");
  if (!state.history.length) { wrap.hidden = true; return; }
  wrap.hidden = false;
  list.innerHTML = "";
  for (const [i, entry] of state.history.entries()) {
    const line = entry.items.map(x => x.tag).join(" / ");
    const li = document.createElement("li");
    const styleTag = styleLabel(entry.style);
    const presetTag = presetLabel(entry.style, entry.preset);
    const timeStr = relativeTime(entry.at);
    li.innerHTML = `
      <button class="history-item" data-idx="${i}" type="button" title="点击复制">
        <div class="history-meta">
          <span class="history-chip">${escapeHTML(styleTag)}</span>
          <span class="history-chip">${escapeHTML(presetTag)}</span>
          <span class="history-time"></span>
        </div>
        <div class="history-tags"></div>
      </button>
    `;
    li.querySelector(".history-time").textContent = timeStr;
    li.querySelector(".history-tags").textContent = line;
    list.appendChild(li);
  }
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// ----------------- switching -----------------
function switchStyle(key) {
  if (!window.TAG_LIBRARY[key]) return;
  state.styleKey = key;
  state.library = window.TAG_LIBRARY[key];
  if (!state.library.presets.find(p => p.key === state.presetKey)) {
    state.presetKey = state.library.presets[0].key;
  }
  try {
    localStorage.setItem(STYLE_KEY, key);
    localStorage.setItem(PRESET_KEY, state.presetKey);
  } catch {}
  renderStyleSwitch();
  renderPresets();
  rollAll();
}

function switchPreset(key) {
  state.presetKey = key;
  try { localStorage.setItem(PRESET_KEY, key); } catch {}
  renderPresets();
  rollAll();
}

// ----------------- copy feedback -----------------
async function flashCopy(btn, text, doneLabel) {
  const ok = await clipboardCopy(text);
  const old = btn.dataset.origLabel || btn.textContent;
  btn.dataset.origLabel = old;
  btn.textContent = ok ? (doneLabel || "已复制") : "复制失败";
  btn.classList.add(ok ? "copied" : "failed");
  setTimeout(() => {
    btn.textContent = old;
    btn.classList.remove("copied", "failed");
    delete btn.dataset.origLabel;
  }, 1100);
}

// ----------------- AI generate -----------------
async function generateStory(isRegen) {
  const btn = $("#generateBtn");
  const story = $("#story");
  const body = $("#storyBody");
  const meta = $("#storyMeta");

  if (!Object.keys(state.current).length) return;

  const tagsForAi = {};
  for (const dim of activeDims()) {
    if (state.current[dim.key]) tagsForAi[dim.label] = state.current[dim.key];
  }

  story.hidden = false;
  body.classList.remove("error");
  body.textContent = "生成中…";
  meta.textContent = "";

  btn.disabled = true;
  btn.classList.add("loading");
  const span = btn.querySelector("span");
  const orig = span.textContent;
  span.textContent = isRegen ? "重写中…" : "生成中…";

  const t0 = Date.now();
  try {
    const r = await fetch("/api/play/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tags: tagsForAi })
    });
    if (r.status === 401) { location.href = "/"; return; }
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
    // 切段渲染：每段 <p>，段间距用 CSS 控制
    const text = (data.text || "").trim();
    body.innerHTML = "";
    const paragraphs = text.split(/\n\s*\n+/).filter(Boolean);
    for (const para of paragraphs) {
      const p = document.createElement("p");
      p.textContent = para.replace(/\n+/g, " ").trim();
      body.appendChild(p);
    }
    const secs = Math.round((Date.now() - t0) / 100) / 10;
    const tokens = data.usage?.total_tokens;
    meta.textContent = `${secs}s · ${data.model || "deepseek-chat"}${tokens ? ` · ${tokens} tokens` : ""}`;
  } catch (err) {
    body.classList.add("error");
    body.textContent = `[生成失败] ${err.message}\n\n如果本地双击 index.html 打开，文字板用不了——这功能需要 Cloudflare 后端。线上 https://playroll.mininicole.com 可用。`;
  } finally {
    btn.disabled = false;
    btn.classList.remove("loading");
    span.textContent = orig;
  }
}

async function flashCopyChip(chip, text) {
  const ok = await clipboardCopy(text);
  chip.classList.add(ok ? "copied" : "failed");
  setTimeout(() => {
    chip.classList.remove("copied", "failed");
  }, 800);
}

// ----------------- init -----------------
function init() {
  if (!window.TAG_LIBRARY) throw new Error("tags.js 没加载");
  const libs = Object.keys(window.TAG_LIBRARY);
  let savedStyle = null, savedPreset = null;
  try {
    savedStyle = localStorage.getItem(STYLE_KEY);
    savedPreset = localStorage.getItem(PRESET_KEY);
  } catch {}
  state.styleKey = (savedStyle && libs.includes(savedStyle)) ? savedStyle : libs[0];
  state.library = window.TAG_LIBRARY[state.styleKey];
  const presetExists = state.library.presets.find(p => p.key === savedPreset);
  state.presetKey = presetExists ? savedPreset : state.library.presets[0].key;
  loadHistory();

  renderStyleSwitch();
  renderPresets();
  renderHistory();
  rollAll(true); // 初始不写历史

  $("#roll").addEventListener("click", () => rollAll());

  $("#copy").addEventListener("click", (e) => {
    flashCopy(e.currentTarget, copyAllText(), "已复制");
  });

  $("#styleSwitch").addEventListener("click", (e) => {
    const btn = e.target.closest(".style-btn");
    if (btn) switchStyle(btn.dataset.style);
  });
  $("#presets").addEventListener("click", (e) => {
    const btn = e.target.closest(".preset-btn");
    if (btn) switchPreset(btn.dataset.preset);
  });
  $("#result").addEventListener("click", (e) => {
    const btn = e.target.closest(".reroll");
    if (btn) rerollOne(btn.dataset.dim);
  });
  $("#chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) flashCopyChip(chip, chip.dataset.tag);
  });
  $("#historyList").addEventListener("click", async (e) => {
    const btn = e.target.closest(".history-item");
    if (!btn) return;
    const entry = state.history[Number(btn.dataset.idx)];
    if (!entry) return;
    const text = entry.items.map(x => x.tag).join(" / ");
    const ok = await clipboardCopy(text);
    btn.classList.add(ok ? "copied" : "failed");
    setTimeout(() => btn.classList.remove("copied", "failed"), 800);
  });
  $("#clearHistory").addEventListener("click", clearHistory);

  $("#generateBtn").addEventListener("click", () => generateStory(false));
  $("#storyRegen").addEventListener("click", () => generateStory(true));
  $("#storyClose").addEventListener("click", () => { $("#story").hidden = true; });
  $("#storyCopy").addEventListener("click", (e) => {
    flashCopy(e.currentTarget, $("#storyBody").textContent || "", "已复制");
  });

  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && e.target.tagName !== "BUTTON" && e.target.tagName !== "INPUT") {
      e.preventDefault();
      rollAll();
    }
  });

  // 每分钟刷新一次历史的相对时间
  setInterval(() => {
    if (!$("#historySection").hidden) renderHistory();
  }, 60_000);
}

try { init(); } catch (err) {
  document.body.insertAdjacentHTML("beforeend",
    `<pre style="color:#c96442;padding:20px">载入失败：${err.message}</pre>`);
}
