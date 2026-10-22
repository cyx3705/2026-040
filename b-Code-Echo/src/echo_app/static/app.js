"use strict";

const $ = (id) => document.getElementById(id);
const log = $("log"), input = $("input"), send = $("send"), form = $("form");
let history = [];   // 发给引擎的对话：{role, content}
let convId = null;  // 当前对话在历史里的编号；新对话第一轮说完才有
let busy = false;
let ready = false;
let switching = false;  // 正在换模型
let level = "cefr:A2";
try { level = localStorage.getItem("echo-level") || level; } catch (e) {}
let replyLength = 0;  // 回答长度：0 自动（跟着难度），1～5 从一句到长篇
try { replyLength = Number(localStorage.getItem("echo-length")) || 0; } catch (e) {}
const learned = new Set();   // 学会的词（lemma）
const WORD = /[A-Za-z]+(?:['’][A-Za-z]+)*/g;

// 主题：记住选择
$("theme").addEventListener("click", () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("echo-theme", t); } catch (e) {}
});

$("new-chat").addEventListener("click", () => {
  if (busy) return;
  history = [];
  convId = null;
  setPlan(null);
  log.replaceChildren($("empty") || makeEmpty());
  setTitle(null);
  markCurrent();
  closeSide();
  input.focus();
  scheduleSuggest();
});

function makeEmpty() {
  const box = document.createElement("div");
  box.className = "empty"; box.id = "empty";
  box.innerHTML = '<p class="empty-title">用英语说点什么吧</p><p class="empty-note">AI 会用简单的英语回你。说不完整也没关系。</p>';
  return box;
}

// 引擎状态
async function checkEngine() {
  const el = $("engine");
  try {
    const s = await (await fetch("/api/status")).json();
    ready = s.state === "ready";
    el.dataset.state = ready ? "ready" : "error";
    $("engine-text").textContent = ready ? "本地引擎就绪" : s.detail;
    if (s.model) $("model-note").textContent = `${s.model} · 本地运行`;
    if (s.levels) renderLevels(s.levels);
    if (s.lengths) renderLengths(s.lengths);
  } catch (e) {
    ready = false;
    el.dataset.state = "error";
    $("engine-text").textContent = "连不上 Echo 后端";
  }
  updateSend();
  if (ready) scheduleSuggest();
  else setTimeout(checkEngine, 3000);
}

function updateSend() { send.disabled = busy || switching || !ready || !input.value.trim(); }

function addMessage(kind, text) {
  $("empty")?.remove();
  const box = document.createElement("div");
  box.className = `msg msg-${kind}`;
  const role = document.createElement("span");
  role.className = "msg-role";
  role.textContent = kind === "me" ? "YOU" : kind === "ai" ? "ECHO" : "ERROR";
  const body = document.createElement("div");
  body.className = "msg-body";
  body.textContent = text;
  box.append(role, body);
  log.append(box);
  scrollToBottom(true);
  return box;
}

// 滚到页底，而不是 scrollIntoView：输入区吸在底部，scrollIntoView 把消息对齐到视口底边，
// 正好被输入区挡住。页底留着输入区的高度，滚到底最后一条消息就在输入区上方。
function scrollToBottom(smooth) {
  window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

// 用户往上翻着看旧消息时，不把他拽回来
function nearBottom() {
  const d = document.documentElement;
  return d.scrollHeight - (window.scrollY + window.innerHeight) < 160;
}

async function submit() {
  const text = input.value.trim();
  if (!text || busy || switching || !ready) return;
  input.value = ""; autosize();
  addMessage("me", text);
  history.push({ role: "user", content: text });
  await aiTurn();
}

// AI 说一轮。练词对话刚开始时 history 是空的：AI 先开口
async function aiTurn() {
  busy = true;
  setControls();
  hideSuggest();
  updateSend();
  const box = addMessage("ai", "");
  box.classList.add("pending");
  const body = box.querySelector(".msg-body");
  let reply = "";
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history, level, length: replyLength, conversation_id: convId, practice: plan }),
    });
    const reader = resp.body.getReader();
    let saved = null;
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, cut); buf = buf.slice(cut + 2);
        if (!line.startsWith("data: ")) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.error) throw new Error(ev.error);
        if (ev.done) saved = ev;
        if (ev.delta) {
          reply += ev.delta;
          const stick = nearBottom();
          body.textContent = reply.replace(/^\s+/, "");
          if (stick) scrollToBottom(false);
        }
      }
    }
    history.push({ role: "assistant", content: reply.trim() });
    if (saved) {
      const fresh = convId == null;
      convId = saved.conversation_id;
      if (plan) { planUsed = new Set(saved.used || []); renderTargets(); }
      loadConversations();  // 新对话先以第一句话为名进列表，顶到最上面
      if (fresh && !plan) nameConversation(convId);  // 练词对话的标题是固定的「练词 · 分组」
    }
    await renderGlossed(body, reply.trim());
  } catch (e) {
    box.remove();
    if (history.at(-1)?.role === "user") history.pop();  // 这一轮没成，撤回用户那句
    addMessage("error", `没有得到回复：${e.message}`);
  } finally {
    box.classList.remove("pending");
    busy = false;
    setControls();
    updateSend();
    input.focus();
    fetchSuggest();  // AI 说完了：给出回话的第一个词
  }
}

form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
input.addEventListener("keydown", (e) => {
  if (e.isComposing) return;
  const k = e.key.toLowerCase();
  // Shift+W / Shift+S 在候选里左右移动；候选没出来时照常打出大写 W/S
  if (e.shiftKey && (k === "w" || k === "s") && suggestions.length) {
    e.preventDefault();
    const n = suggestions.length;
    sel = k === "s" ? (sel + 1) % n : (sel - 1 + n) % n;
    renderSuggest();
    return;
  }
  // Tab 确认选中的词（默认第一个）；Enter 只管发送，两者不再抢一个键
  if (e.key === "Tab" && !e.shiftKey && suggestions.length) {
    e.preventDefault();
    accept(suggestions[sel]);
    return;
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
});

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight + 2, 180) + "px";
  input.style.overflowY = input.scrollHeight + 2 > 180 ? "auto" : "hidden";  // 没到上限不出滚动条
}
input.addEventListener("input", () => { autosize(); updateSend(); scheduleSuggest(); });
input.addEventListener("focus", () => { if (!suggestions.length) scheduleSuggest(); });

// ---- 输入预测：每打完一个词（或打到一半）跑一次前向，列出 5 个整词候选 ----

let suggestions = [];
let sel = 0;
let suggestTimer = 0;
let suggestSeq = 0;
const PARTIAL = /[A-Za-z']+$/;

function scheduleSuggest() {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(fetchSuggest, 90);
}

async function fetchSuggest() {
  if (busy || switching || !ready) { hideSuggest(); return; }
  const text = input.value;
  const seq = ++suggestSeq;
  $("suggest").classList.add("loading");
  try {
    const resp = await fetch("/api/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history, text, level, length: replyLength, practice: plan }),
    });
    const items = await resp.json();
    if (seq !== suggestSeq || input.value !== text || busy) return;  // 已经过时
    suggestions = Array.isArray(items) ? items : [];
    sel = 0;
    renderSuggest();
  } catch (e) {
    if (seq === suggestSeq) hideSuggest();
  } finally {
    if (seq === suggestSeq) $("suggest").classList.remove("loading");
  }
}

function hideSuggest() {
  suggestions = []; sel = 0;
  renderSuggest();
}

// 候选行常驻：没有候选时显示一行说明，行高不变，输入框不跳
function renderSuggest() {
  const list = $("suggest-list");
  if (!suggestions.length) {
    const note = document.createElement("li");
    note.className = "suggest-empty";
    note.setAttribute("aria-hidden", "true");
    note.textContent = switching ? "正在换模型…" : busy ? "AI 正在说…" : ready ? "预测下一个词…" : "引擎还没就绪";
    list.replaceChildren(note);
    return;
  }
  list.replaceChildren(...suggestions.map((s, i) => {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === sel ? "true" : "false");
    li.innerHTML = '<span class="top"><span class="no"></span><span class="word"></span></span><span class="gloss"></span>';
    li.querySelector(".no").textContent = i + 1;
    li.querySelector(".word").textContent = s.word;
    li.querySelector(".gloss").textContent = s.gloss || "";
    li.addEventListener("mousedown", (e) => { e.preventDefault(); accept(s); });  // 不让输入框失焦
    return li;
  }));
}

function accept(s) {
  const text = input.value;
  const partial = /\s$/.test(text) ? "" : (text.match(PARTIAL) || [""])[0];
  let base = text.slice(0, text.length - partial.length);
  if (s.kind === "punct") {
    base = base.trimEnd();                  // 标点贴着前一个词
  } else if (base && !/\s$/.test(base)) {
    base += " ";                            // 上一个是标点：先空一格
  }
  input.value = base + s.word + " ";        // 空格＝这个词说完了
  input.setSelectionRange(input.value.length, input.value.length);
  autosize(); updateSend();
  fetchSuggest();
}

// ---- 逐词释义：AI 回复说完后，每个词下面标中文 ----

async function renderGlossed(body, text) {
  const words = [...new Set((text.match(WORD) || []))];
  let entries = {};
  try {
    const resp = await fetch("/api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ words }),
    });
    entries = await resp.json();
  } catch (e) {
    return;  // 查不到词典就保留纯文本
  }
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(WORD)) {
    if (m.index > last) frag.append(sep(text.slice(last, m.index)));
    frag.append(wordEl(m[0], entries[m[0]]));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(sep(text.slice(last)));
  const stick = nearBottom();
  body.replaceChildren(frag);  // 加了中文行，消息变高
  if (stick) scrollToBottom(true);
}

function sep(s) {
  const el = document.createElement("span");
  el.className = "sep";
  el.textContent = s;
  return el;
}

function wordEl(word, entry) {
  if (!entry) {  // 词典里没有（人名、生造词）：只显示原文
    const el = document.createElement("span");
    el.className = "plain";
    el.textContent = word;
    return el;
  }
  const el = document.createElement("span");
  el.className = "w" + (learned.has(entry.lemma) ? " learned" : "")
    + (planLemmas.has(entry.lemma) || planLemmas.has(word.toLowerCase()) ? " target" : "");
  el.dataset.lemma = entry.lemma;
  el.dataset.gloss = entry.gloss;
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.title = [entry.phonetic && `/${entry.phonetic}/`, entry.translation].filter(Boolean).join("\n");
  const en = document.createElement("span");
  en.className = "w-en";
  en.textContent = word;
  const zh = document.createElement("span");
  zh.className = "w-zh";
  zh.textContent = entry.gloss;
  el.append(en, zh);
  return el;
}

// 点一下：有中文的记为学会、中文收起；没中文的重新带上中文
log.addEventListener("click", (e) => {
  const el = e.target.closest(".w");
  if (el) toggleLearned(el.dataset.lemma);
});
log.addEventListener("keydown", (e) => {
  const el = e.target.closest(".w");
  if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleLearned(el.dataset.lemma); }
});

function toggleLearned(lemma) {
  return setLearned(lemma, !learned.has(lemma));
}

async function setLearned(lemma, on) {
  on ? learned.add(lemma) : learned.delete(lemma);
  for (const el of log.querySelectorAll(".w")) {
    if (el.dataset.lemma === lemma) el.classList.toggle("learned", on);
  }
  updateCount();
  try {
    await fetch("/api/learned", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lemma, learned: on }),
    });
  } catch (e) {}
}

function updateCount() {
  $("learned-count").textContent = learned.size;
  $("dict-count").textContent = learned.size;
}

async function loadLearned() {
  try {
    const list = await (await fetch("/api/learned")).json();
    list.forEach((x) => learned.add(x.lemma));
    updateCount();
  } catch (e) {}
}

// ---- 词典面板：看学会了哪些词 ----

const dict = $("dict");
$("open-dict").addEventListener("click", openDict);
$("close-dict").addEventListener("click", () => dict.close());
dict.addEventListener("click", (e) => { if (e.target === dict) dict.close(); });

async function openDict() {
  let list = [];
  try { list = await (await fetch("/api/learned")).json(); } catch (e) {}
  learned.clear();
  list.forEach((x) => learned.add(x.lemma));
  updateCount();
  const ul = $("dict-list");
  ul.replaceChildren(...list.map((x) => {
    const li = document.createElement("li");
    const w = document.createElement("span"); w.className = "dict-word"; w.textContent = x.lemma;
    const g = document.createElement("span"); g.className = "dict-gloss"; g.textContent = x.gloss;
    const b = document.createElement("button"); b.className = "forget"; b.textContent = "没学会";
    b.addEventListener("click", async () => { await setLearned(x.lemma, false); li.remove(); toggleDictEmpty(); });
    li.append(w, g, b);
    return li;
  }));
  toggleDictEmpty();
  dict.showModal();
}

function toggleDictEmpty() {
  const none = !$("dict-list").children.length;
  $("dict-list").hidden = none;
  $("dict-empty").hidden = !none;
}

// ---- 难度：只改 AI 怎么说话，跟着每段对话存；换了从下一句起生效 ----

// 难度目录来自后端：评价标准（CEFR、四六级、专四专八、雅思口语）→ 档位
let standards = [];
const levelById = new Map();
const LEGACY_LEVELS = { starter: "cefr:A1", basic: "cefr:A2", intermediate: "cefr:B1", advanced: "cefr:C1" };
const levelBtn = $("level-btn"), levelPanel = $("level-panel");
let panelStd = null;  // 面板里正在看的标准

function renderLevels(list) {
  standards = list;
  levelById.clear();
  for (const std of standards) for (const l of std.levels) levelById.set(l.id, { ...l, std });
  level = normLevel(level);
  showLevel();
}

function normLevel(id) {
  id = LEGACY_LEVELS[id] || id;
  return levelById.has(id) ? id : (levelById.has("cefr:A2") ? "cefr:A2" : [...levelById.keys()][0]);
}

// 按钮上的简称：CEFR A2、CET-4、专四、雅思 6.5
function levelShort(l) {
  if (l.std.id === "cefr") return `CEFR ${l.name}`;
  if (l.std.id === "ielts") return `雅思 ${l.name}`;
  return l.name;
}

function showLevel() {
  const l = levelById.get(level);
  if (!l) return;
  $("level-value").textContent = levelShort(l);
  levelBtn.title = `对话难度：${l.std.name} ${l.name}（${l.note}）`;
  if (!levelPanel.hidden) renderLevelPanel();
}

function renderLevelPanel() {
  const cur = levelById.get(level);
  panelStd = panelStd || cur.std.id;
  $("level-tabs").replaceChildren(...standards.map((std) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(std.id === panelStd));
    b.textContent = std.name;
    b.addEventListener("click", () => { panelStd = std.id; renderLevelPanel(); });
    return b;
  }));
  const std = standards.find((s) => s.id === panelStd);
  const note = $("level-note");
  const say = (l) => { note.replaceChildren(); const b = document.createElement("b"); b.textContent = `${std.name} ${l.name}`; note.append(b, `　${l.note}`); };
  $("level-grid").replaceChildren(...std.levels.map((l) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(l.id === level));
    b.textContent = l.name;
    b.addEventListener("mouseenter", () => say(l));
    b.addEventListener("focus", () => say(l));
    b.addEventListener("click", () => { setLevel(l.id); closeLevelPanel(); input.focus(); });
    return b;
  }));
  const shown = std.levels.find((l) => l.id === level);
  if (shown) say(shown);
  else { note.textContent = `当前：${cur.std.name} ${cur.name}`; }
  $("level-grid").onmouseleave = () => { if (shown) say(shown); else note.textContent = `当前：${cur.std.name} ${cur.name}`; };
}

levelBtn.addEventListener("click", () => (levelPanel.hidden ? openLevelPanel() : closeLevelPanel()));
document.addEventListener("click", (e) => {
  // 用 composedPath：点标准页签会重画面板，被点的按钮已经不在文档里，closest 找不到外层
  if (!levelPanel.hidden && !e.composedPath().includes(levelBtn.parentElement)) closeLevelPanel();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !levelPanel.hidden) { closeLevelPanel(); levelBtn.focus(); }
});

function openLevelPanel() {
  if (!standards.length) return;
  panelStd = levelById.get(level).std.id;
  levelPanel.hidden = false;
  levelBtn.setAttribute("aria-expanded", "true");
  renderLevelPanel();
}

function closeLevelPanel() {
  levelPanel.hidden = true;
  levelBtn.setAttribute("aria-expanded", "false");
}

function setLevel(id) {
  if (busy || id === level) return;
  level = id;
  try { localStorage.setItem("echo-level", id); } catch (e) {}
  showLevel();
  scheduleSuggest();  // 系统提示词变了，候选也跟着变
}

// ---- 回答长度：难度面板底部的滑块，管 AI 一次说多少；用词还是听难度的 ----

let lengths = [];
const lengthSlider = $("length");

function renderLengths(list) {
  lengths = list;
  lengthSlider.max = lengths.length - 1;
  if (!lengths[replyLength]) replyLength = 0;
  lengthSlider.value = replyLength;
  $("length-ticks").replaceChildren(...lengths.map((l) => {
    const t = document.createElement("span");
    t.textContent = l.name;
    t.addEventListener("click", () => setLength(l.id));
    return t;
  }));
  showLength();
}

function showLength() {
  const l = lengths[replyLength];
  if (!l) return;
  $("length-name").textContent = l.name;
  $("length-note").textContent = l.note;
  $("level-length").textContent = `长度 ${l.name}`;
  lengthSlider.setAttribute("aria-valuetext", `${l.name}：${l.note}`);
  [...$("length-ticks").children].forEach((t, i) => t.classList.toggle("on", i === replyLength));
}

function setLength(n) {
  if (busy || n === replyLength) return;
  replyLength = n;
  lengthSlider.value = n;
  try { localStorage.setItem("echo-length", String(n)); } catch (e) {}
  showLength();
  scheduleSuggest();  // 系统提示词变了，候选也跟着变
}

lengthSlider.addEventListener("input", () => setLength(Number(lengthSlider.value)));

// AI 说话或换模型的时候，难度和模型都先别动
function setControls() {
  levelBtn.disabled = busy || switching;
  if (levelBtn.disabled) closeLevelPanel();
  $("model-btn").disabled = busy || switching;
}

// ---- 模型：墨色条里点模型名换 ----

const modelBtn = $("model-btn"), modelMenu = $("model-menu");
modelBtn.addEventListener("click", () => (modelMenu.hidden ? openModelMenu() : closeModelMenu()));
document.addEventListener("click", (e) => {
  if (!modelMenu.hidden && !e.target.closest(".ink-bar")) closeModelMenu();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modelMenu.hidden) { closeModelMenu(); modelBtn.focus(); } });

function closeModelMenu() {
  modelMenu.hidden = true;
  modelBtn.setAttribute("aria-expanded", "false");
}

async function openModelMenu() {
  let data;
  try { data = await (await fetch("/api/models")).json(); } catch (e) { return; }
  const gb = (n) => (n / 2 ** 30).toFixed(1) + " GB";
  const items = data.models.map((m) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "model-opt";
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", m.name === data.current ? "true" : "false");
    b.disabled = !m.supported;
    const note = !m.supported ? "对话模板不认得，没法做输入预测"
      : m.installed ? "已装进 Echo" : "从本机 Ollama 链接过来，不占额外空间";
    b.innerHTML = '<span class="model-name"></span><span class="model-meta"></span>';
    b.querySelector(".model-name").textContent = m.name;
    b.querySelector(".model-meta").textContent = `${gb(m.size)} · ${note}`;
    b.addEventListener("click", () => { closeModelMenu(); if (m.name !== data.current) switchModel(m.name); });
    return b;
  });
  const tip = document.createElement("p");
  tip.className = "model-menu-note";
  tip.textContent = "同一时间只放一个模型在显存里，换模型要加载几秒到几十秒。";
  modelMenu.replaceChildren(...items, tip);
  modelMenu.hidden = false;
  modelBtn.setAttribute("aria-expanded", "true");
}

async function switchModel(name) {
  switching = true;
  setControls(); updateSend(); hideSuggest();
  $("model-note").textContent = `正在加载 ${name}…`;
  $("engine").dataset.state = "checking";
  $("engine-text").textContent = "正在换模型";
  let err = "";
  try {
    const resp = await fetch("/api/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) err = (await resp.json()).detail || `换模型失败（${resp.status}）`;
  } catch (e) {
    err = e.message;
  }
  switching = false;
  setControls();
  if (err) addMessage("error", `没能换成 ${name}：${err}`);
  await checkEngine();
  input.focus();
}

// ---- 对话历史：左侧列表，可搜索；第一轮说完自动起名 ----

const convList = $("conv-list"), search = $("search");
let searchTimer = 0;
search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadConversations, 150); });

async function loadConversations() {
  const q = search.value.trim();
  let list = [];
  try { list = await (await fetch("/api/conversations?q=" + encodeURIComponent(q))).json(); } catch (e) { return; }
  if (q !== search.value.trim()) return;  // 已经过时
  convList.replaceChildren(...list.map((c) => convItem(c, q)));
  const empty = $("conv-empty");
  empty.hidden = list.length > 0;
  empty.textContent = q ? `没有包含「${q}」的对话` : "还没有对话。说完第一句，它就会出现在这里。";
  markCurrent();
}

function convItem(c, q) {
  const li = document.createElement("li");
  const b = document.createElement("button");
  b.className = "conv";
  b.dataset.id = c.id;
  const t = document.createElement("span"); t.className = "conv-title"; t.textContent = c.title;
  const m = document.createElement("span"); m.className = "conv-meta";
  if (c.snippet) highlight(m, c.snippet, q); else m.textContent = when(c.updated);
  b.append(t, m);
  b.addEventListener("click", () => openConversation(c.id));
  const del = document.createElement("button");
  del.className = "conv-del";
  del.setAttribute("aria-label", `删除「${c.title}」`);
  del.title = "删除这段对话";
  del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  del.addEventListener("click", () => deleteConversation(c));
  li.append(b, del);
  return li;
}

function highlight(el, text, q) {
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) { el.textContent = text; return; }
  const mark = document.createElement("mark");
  mark.textContent = text.slice(i, i + q.length);
  el.append(text.slice(0, i), mark, text.slice(i + q.length));
}

function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `今天 ${hm}`;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `昨天 ${hm}`;
  return d.getFullYear() === now.getFullYear()
    ? `${d.getMonth() + 1}月${d.getDate()}日` : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

function markCurrent() {
  for (const b of convList.querySelectorAll(".conv")) {
    b.setAttribute("aria-current", String(Number(b.dataset.id) === convId));
  }
}

function setTitle(title) {
  $("chat-title").textContent = title || "对话";
  document.title = title ? `${title} · Echo` : "Echo";
}

async function openConversation(id) {
  if (busy || switching) return;
  let conv;
  try {
    const resp = await fetch(`/api/conversations/${id}`);
    if (!resp.ok) { loadConversations(); return; }
    conv = await resp.json();
  } catch (e) { return; }
  convId = conv.id;
  history = conv.messages;
  await setPlan(conv.practice, conv.used);
  if (levelById.size) { level = normLevel(conv.level); showLevel(); }
  setTitle(conv.title);
  markCurrent();
  closeSide();
  log.replaceChildren();
  if (!history.length) log.append(makeEmpty());
  const glossing = [];
  for (const m of history) {
    const box = addMessage(m.role === "user" ? "me" : "ai", m.content);
    if (m.role === "assistant") glossing.push(renderGlossed(box.querySelector(".msg-body"), m.content));
  }
  await Promise.all(glossing);
  scrollToBottom(false);
  input.value = ""; autosize(); updateSend();
  input.focus();
  fetchSuggest();
}

async function nameConversation(id) {
  try {
    const { title } = await (await fetch(`/api/conversations/${id}/title`, { method: "POST" })).json();
    if (id === convId) setTitle(title);
    loadConversations();
  } catch (e) {}
}

async function deleteConversation(c) {
  if (busy || !confirm(`删除「${c.title}」？删了就找不回来了。`)) return;
  try { await fetch(`/api/conversations/${c.id}`, { method: "DELETE" }); } catch (e) { return; }
  if (c.id === convId) $("new-chat").click();
  loadConversations();
}

// ---- 练词：从单词表挑几个词，整段对话围着它们练 ----

let plan = null;              // 当前对话的练词计划 {list_id, label, words: [{word, meaning}]}；普通对话为 null
let planUsed = new Set();     // 学习者已经亲口用上的词（后端判定，按原形算）
let planLemmas = new Set();   // 练习词的原形：AI 回复里出现就高亮

async function setPlan(p, used = []) {
  plan = p && p.words?.length ? p : null;
  planUsed = new Set(used);
  planLemmas = new Set();
  if (plan) {
    const words = plan.words.map((w) => w.word);
    words.forEach((w) => planLemmas.add(w.toLowerCase()));
    try {
      const entries = await (await fetch("/api/lookup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ words }),
      })).json();
      for (const e of Object.values(entries)) if (e) planLemmas.add(e.lemma);
    } catch (e) {}
  }
  renderTargets();
}

function renderTargets() {
  $("targets").hidden = !plan;
  if (!plan) return;
  $("targets-count").textContent = `${plan.words.filter((w) => planUsed.has(w.word)).length}/${plan.words.length}`;
  $("target-list").replaceChildren(...plan.words.map((w) => {
    const li = document.createElement("li");
    li.className = planUsed.has(w.word) ? "used" : "";
    li.title = planUsed.has(w.word) ? `${w.word}：你已经用上了` : `${w.word}：还没用上，试着在回答里说出来`;
    li.innerHTML = '<span class="t-word"></span><span class="t-gloss"></span>';
    li.querySelector(".t-word").textContent = w.word;
    li.querySelector(".t-gloss").textContent = w.meaning || "";
    return li;
  }));
}

// 单词表面板：导入 Word、选表、选组、挑词、开始练习
const PICK_MAX = 12;     // 一段对话塞太多词，AI 照顾不过来
const PICK_DEFAULT = 8;  // 默认先挑这么多
const wordsDlg = $("words");
let wlLists = [], wlCur = null, wlGroup = null, wlPicked = new Set();

$("open-words").addEventListener("click", openWords);
$("close-words").addEventListener("click", () => wordsDlg.close());
wordsDlg.addEventListener("click", (e) => { if (e.target === wordsDlg) wordsDlg.close(); });

async function openWords() {
  $("import-note").hidden = true;
  await loadWordlists(wlCur?.id);
  wordsDlg.showModal();
}

async function loadWordlists(selectId) {
  try { wlLists = await (await fetch("/api/wordlists")).json(); } catch (e) { wlLists = []; }
  const none = !wlLists.length;
  $("words-empty").hidden = !none;
  $("word-grid").hidden = none; $("groups").hidden = none; $("words-foot").hidden = none;
  const total = wlLists.reduce((n, l) => n + l.count, 0);
  $("words-summary").textContent = none ? "从 Word 导入单词表，挑几个词专门练"
    : `${wlLists.length} 个单词表 · ${total} 个词 · 练过 ${wlLists.reduce((n, l) => n + l.practiced, 0)} 个`;
  $("wl-tabs").replaceChildren(...wlLists.map((l) => {
    const tab = document.createElement("div");
    tab.className = "wl-tab";
    tab.setAttribute("role", "tab");
    const pick = document.createElement("button");
    pick.className = "wl-pick";
    pick.innerHTML = '<span></span><span class="count"></span>';
    pick.firstChild.textContent = l.name;
    pick.lastChild.textContent = l.count;
    pick.addEventListener("click", () => selectWordlist(l.id));
    const del = document.createElement("button");
    del.className = "wl-del";
    del.setAttribute("aria-label", `删除单词表「${l.name}」`);
    del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg>';
    del.addEventListener("click", () => deleteWordlist(l));
    tab.append(pick, del);
    tab.dataset.id = l.id;
    return tab;
  }));
  if (!none) await selectWordlist(wlLists.some((l) => l.id === selectId) ? selectId : wlLists[0].id);
}

async function selectWordlist(id) {
  try { wlCur = await (await fetch(`/api/wordlists/${id}`)).json(); } catch (e) { return; }
  for (const t of $("wl-tabs").children) t.setAttribute("aria-selected", String(Number(t.dataset.id) === id));
  const groups = [...new Set(wlCur.words.map((w) => w.group))];
  // 默认停在第一个还有没练过的词的组
  const firstNew = wlCur.words.find((w) => !w.used);
  selectGroup(firstNew ? firstNew.group : groups[0], groups);
}

function selectGroup(g, groups = [...new Set(wlCur.words.map((w) => w.group))]) {
  wlGroup = g;
  const box = $("groups");
  box.hidden = groups.length < 2;
  box.replaceChildren(...groups.map((name) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(name === g));
    const n = wlCur.words.filter((w) => w.group === name).length;
    b.innerHTML = '<span></span><span class="n"></span>';
    b.firstChild.textContent = name || "未分组";
    b.lastChild.textContent = n;
    b.addEventListener("click", () => selectGroup(name, groups));
    return b;
  }));
  box.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  // 默认挑这组里前几个没练过的词
  const inGroup = wlCur.words.filter((w) => w.group === g);
  const fresh = inGroup.filter((w) => !w.used);
  wlPicked = new Set((fresh.length ? fresh : inGroup).slice(0, PICK_DEFAULT).map((w) => w.word));
  renderWordGrid();
}

function renderWordGrid() {
  $("word-grid").replaceChildren(...wlCur.words.filter((w) => w.group === wlGroup).map((w) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-pressed", String(wlPicked.has(w.word)));
    b.innerHTML = '<span class="g-word"><span></span><span class="g-used"></span></span><span class="g-mean"></span>';
    b.querySelector(".g-word span").textContent = w.word;
    b.querySelector(".g-used").textContent = w.used ? `练过 ${w.used}` : "";
    b.querySelector(".g-mean").textContent = w.meaning;
    b.title = w.meaning ? `${w.word}　${w.meaning}` : w.word;
    b.addEventListener("click", () => {
      if (wlPicked.has(w.word)) wlPicked.delete(w.word);
      else if (wlPicked.size < PICK_MAX) wlPicked.add(w.word);
      renderWordGrid();
    });
    li.append(b);
    return li;
  }));
  const n = wlPicked.size;
  $("pick-note").innerHTML = n ? `挑了 <b>${n}</b> 个词${n >= PICK_MAX ? `（一次最多 ${PICK_MAX} 个）` : ""}，点词可以增减` : "点词挑出要练的（一次最多 12 个）";
  $("start-practice").disabled = !n || busy || !ready;
}

$("import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";  // 同一个文件可以再导一次
  if (!file) return;
  const note = $("import-note");
  note.hidden = false;
  note.textContent = `正在读「${file.name}」…`;
  try {
    const resp = await fetch("/api/wordlists", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
      body: await file.arrayBuffer(),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `导入失败（${resp.status}）`);
    note.textContent = `导入了「${data.name}」：${data.count} 个词。`;
    await loadWordlists(data.id);
  } catch (err) {
    note.textContent = err.message;
  }
});

async function deleteWordlist(l) {
  if (!confirm(`删除单词表「${l.name}」？练过的次数也会一起删掉；已经聊过的练词对话还在。`)) return;
  try { await fetch(`/api/wordlists/${l.id}`, { method: "DELETE" }); } catch (e) { return; }
  if (wlCur?.id === l.id) wlCur = null;
  await loadWordlists();
}

$("start-practice").addEventListener("click", async () => {
  if (busy || switching || !ready || !wlPicked.size) return;
  const words = wlCur.words.filter((w) => wlPicked.has(w.word)).map((w) => ({ word: w.word, meaning: w.meaning }));
  const label = wlGroup ? `${wlCur.name} ${wlGroup}` : wlCur.name;
  wordsDlg.close();
  $("new-chat").click();  // 练词总是开一段新对话
  await setPlan({ list_id: wlCur.id, label, words });
  setTitle(`练词 · ${label}`);
  await aiTurn();  // AI 先开口，把话题带到这些词上
});

// 窄屏：侧栏收起，点左上角按钮滑出
const side = $("side");
$("side-toggle").addEventListener("click", () => {
  const open = side.classList.toggle("open");
  $("side-toggle").setAttribute("aria-expanded", String(open));
  if (open) search.focus();
});
function closeSide() {
  side.classList.remove("open");
  $("side-toggle").setAttribute("aria-expanded", "false");
}

renderSuggest();
loadLearned();
loadConversations();
checkEngine();
input.focus();
