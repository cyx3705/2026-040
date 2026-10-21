"use strict";

const $ = (id) => document.getElementById(id);
const log = $("log"), input = $("input"), send = $("send"), form = $("form");
let history = [];   // 发给引擎的对话：{role, content}
let busy = false;
let ready = false;
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
  log.replaceChildren($("empty") || makeEmpty());
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
  } catch (e) {
    ready = false;
    el.dataset.state = "error";
    $("engine-text").textContent = "连不上 Echo 后端";
  }
  updateSend();
  if (ready) scheduleSuggest();
  else setTimeout(checkEngine, 3000);
}

function updateSend() { send.disabled = busy || !ready || !input.value.trim(); }

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
  if (!text || busy || !ready) return;
  busy = true;
  hideSuggest();
  input.value = ""; autosize(); updateSend();
  addMessage("me", text);
  history.push({ role: "user", content: text });

  const box = addMessage("ai", "");
  box.classList.add("pending");
  const body = box.querySelector(".msg-body");
  let reply = "";
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
    });
    const reader = resp.body.getReader();
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
        if (ev.delta) {
          reply += ev.delta;
          const stick = nearBottom();
          body.textContent = reply.replace(/^\s+/, "");
          if (stick) scrollToBottom(false);
        }
      }
    }
    history.push({ role: "assistant", content: reply.trim() });
    await renderGlossed(body, reply.trim());
  } catch (e) {
    box.remove();
    history.pop();  // 这一轮没成，撤回用户那句，避免对话里出现没有回复的提问
    addMessage("error", `没有得到回复：${e.message}`);
  } finally {
    box.classList.remove("pending");
    busy = false;
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
  if (busy || !ready) { hideSuggest(); return; }
  const text = input.value;
  const seq = ++suggestSeq;
  $("suggest").classList.add("loading");
  try {
    const resp = await fetch("/api/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history, text }),
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
    note.textContent = busy ? "AI 正在说…" : ready ? "预测下一个词…" : "引擎还没就绪";
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
  el.className = "w" + (learned.has(entry.lemma) ? " learned" : "");
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

renderSuggest();
loadLearned();
checkEngine();
input.focus();
