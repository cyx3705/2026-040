"use strict";

const $ = (id) => document.getElementById(id);
const log = $("log"), input = $("input"), send = $("send"), form = $("form");
let history = [];   // 发给引擎的对话：{role, content}
let busy = false;
let ready = false;

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
  if (!ready) setTimeout(checkEngine, 3000);
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
  box.scrollIntoView({ block: "end", behavior: "smooth" });
  return box;
}

async function submit() {
  const text = input.value.trim();
  if (!text || busy || !ready) return;
  busy = true;
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
          body.textContent = reply.replace(/^\s+/, "");
          box.scrollIntoView({ block: "end" });
        }
      }
    }
    history.push({ role: "assistant", content: reply.trim() });
  } catch (e) {
    box.remove();
    history.pop();  // 这一轮没成，撤回用户那句，避免对话里出现没有回复的提问
    addMessage("error", `没有得到回复：${e.message}`);
  } finally {
    box.classList.remove("pending");
    busy = false;
    updateSend();
    input.focus();
  }
}

form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
});

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight + 2, 180) + "px";
}
input.addEventListener("input", () => { autosize(); updateSend(); });

checkEngine();
input.focus();
