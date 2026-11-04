"use strict";

// INPUT-003/005: readonly Chromium textareas suppress the native caret. Mirror
// their text metrics for an overlay caret and sizing without scrollHeight loops.
class EchoComposerView {
  constructor(input, list, count, mode) {
    this.input = input;
    this.list = list;
    this.count = count;
    this.mode = mode;
    this.mirror = document.createElement("div");
    this.mirror.className = "input-mirror";
    this.mirror.setAttribute("aria-hidden", "true");
    this.caret = document.createElement("span");
    this.caret.className = "input-caret";
    this.caret.hidden = true;
    this.caret.setAttribute("aria-hidden", "true");
    input.parentElement.append(this.mirror, this.caret);
    const refresh = () => this.schedule();
    input.addEventListener("keydown", event => this.navigate(event));
    input.addEventListener("pointerdown", () => { this.preferredX = null; });
    input.addEventListener("input", () => { this.preferredX = null; this.reveal = true; });
    for (const name of ["input", "focus", "click", "keyup", "select"])
      input.addEventListener(name, () => {
        for (const animation of this.caret.getAnimations()) animation.currentTime = 0;
        refresh();
      });
    for (const name of ["blur", "scroll"]) input.addEventListener(name, refresh);
    document.addEventListener("selectionchange", refresh);
    window.addEventListener("focus", refresh);
    window.addEventListener("blur", refresh);
    window.addEventListener("resize", refresh);
    this.observer = new ResizeObserver(refresh);
    this.observer.observe(input);
    this.observer.observe(list);
    document.fonts?.ready.then(refresh);
    this.schedule();
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.resize(); });
  }

  navigate(event) {
    const input = this.input, mode = this.mode();
    if (!input.readOnly || input.disabled || mode?.chinese || mode?.composing ||
        event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey ||
        !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const start = input.selectionStart, end = input.selectionEnd;
    const focus = input.selectionDirection === "backward" ? start : end;
    const anchor = input.selectionDirection === "backward" ? end : start;
    const points = [0];
    for (const character of input.value) points.push(points.at(-1) + character.length);
    let target = focus;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const left = event.key === "ArrowLeft";
      target = !event.shiftKey && start !== end ? (left ? start : end) :
        left ? (points.filter(p => p < focus).at(-1) ?? 0) : (points.find(p => p > focus) ?? input.value.length);
      this.preferredX = null;
    } else {
      this.resize();
      this.mirror.textContent = input.value + "\u200b";
      const range = document.createRange(), text = this.mirror.firstChild;
      const position = p => {
        range.setStart(text, p); range.setEnd(text, p);
        return range.getBoundingClientRect();
      };
      const current = position(focus), line = parseFloat(getComputedStyle(input).lineHeight);
      const horizontal = event.key === "Home" || event.key === "End";
      if (horizontal) {
        const row = points.filter(p => Math.abs(position(p).top - current.top) < line / 2);
        target = event.key === "Home" ? row[0] : row.at(-1);
        this.preferredX = null;
      } else {
        const x = this.preferredX ?? current.left;
        this.preferredX = x;
        const y = current.top + (event.key === "ArrowUp" ? -line : line);
        let closest = Infinity;
        for (const p of points) {
          const rect = position(p);
          if (Math.abs(rect.top - y) >= line / 2) continue;
          const distance = Math.abs(rect.left - x);
          if (distance < closest) { closest = distance; target = p; }
        }
        if (closest === Infinity) target = event.key === "ArrowUp" ? 0 : input.value.length;
      }
      range.detach();
    }
    if (event.shiftKey) input.setSelectionRange(Math.min(anchor, target), Math.max(anchor, target), target < anchor ? "backward" : "forward");
    else input.setSelectionRange(target, target);
    this.reveal = true;
    this.schedule();
  }

  resize() {
    const input = this.input, style = getComputedStyle(input);
    // Reserve every configured slot, including while empty, loading or Chinese.
    const columns = getComputedStyle(this.list).gridTemplateColumns.split(" ").length;
    const rows = Math.ceil(this.count() / columns);
    this.list.style.height = Math.min(rows * 46 + (rows - 1) * 6, window.innerHeight * .3) + "px";
    for (const name of ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight",
      "letterSpacing", "textIndent", "textAlign", "tabSize", "paddingTop", "paddingBottom", "paddingLeft", "paddingRight"])
      this.mirror.style[name] = style[name];
    const line = parseFloat(style.lineHeight);
    const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    // INPUT-008: configured rows are a minimum. Measure real wrapped text in
    // the same width; a zero-width tail preserves the final empty newline.
    this.mirror.style.width = input.clientWidth + "px";
    this.mirror.textContent = input.value + "\u200b";
    const desired = Math.max(46, input.rows * line + padding, this.mirror.scrollHeight);
    const height = Math.ceil(desired) + "px";
    if (input.style.height !== height) input.style.height = height;
    input.style.overflowY = "hidden";
    const mode = this.mode();
    this.caret.hidden = !input.readOnly || input.disabled || document.activeElement !== input ||
      !document.hasFocus() || document.hidden || input.selectionStart !== input.selectionEnd ||
      !!mode?.chinese || !!mode?.composing;
    if (!input.readOnly || input.disabled) return;
    // Re-read width after a scrollbar appears. Match the complete text so a
    // caret in the middle of a wrapped word uses the same line as the textarea.
    this.mirror.style.width = input.clientWidth + "px";
    const marker = document.createElement("span");
    marker.textContent = "\u200b";
    this.mirror.replaceChildren(document.createTextNode(input.value.slice(0, input.selectionStart)),
      marker, document.createTextNode(input.value.slice(input.selectionEnd) + "\u200b"));
    const bounds = marker.getBoundingClientRect(), origin = this.mirror.getBoundingClientRect();
    if (this.reveal) {
      const top = bounds.top - origin.top;
      if (top < input.scrollTop) input.scrollTop = top;
      else if (top + bounds.height > input.scrollTop + input.clientHeight)
        input.scrollTop = top + bounds.height - input.clientHeight;
      this.reveal = false;
    }
    const x = bounds.left - origin.left - input.scrollLeft;
    const y = bounds.top - origin.top - input.scrollTop;
    this.caret.style.left = input.offsetLeft + x + "px";
    this.caret.style.top = input.offsetTop + y + "px";
    this.caret.style.height = bounds.height + "px";
    this.caret.hidden ||= x < 0 || x >= input.clientWidth || y < 0 || y + bounds.height > input.clientHeight;
  }
}

// INPUT-001: native bridge owns Windows layout/Ctrl; this class owns DOM
// composition and the focus lease. Ctrl release commits Chinese without translating.
class EchoInputMode {
  constructor(input, hint, changed, view = null) {
    this.input = input;
    this.hint = hint;
    this.changed = changed;
    this.view = view;
    this.owner = 0;
    this.epoch = 0;
    this.chinese = false;
    this.composing = false;
    this.snapshot = null;
    this.baseTitle = document.title;
    this.acquiring = false;
    this.lastMarker = 0;
    this.fallback = true;
    this.undo = [];
    this.proxyMode = false;
    this.proxyPending = false;
    this.committedSeen = "";
    this.proxyAnchor = null;
    input.readOnly = true; // No IME can open before native English mode is ready.
    input.addEventListener("focus", () => this.start());
    input.addEventListener("pointerdown", () => this.start());
    // Native focus checks revoke a proxy lease on the next timer tick. Let its
    // final snapshot deliver confirmed text before clearing the DOM anchor.
    input.addEventListener("blur", () => { if (!this.proxyMode) this.stop(); });
    window.addEventListener("blur", () => { if (!this.proxyMode) this.stop(); });
    window.addEventListener("focus", () => { if (this.focused()) this.start(); });
    window.addEventListener("pagehide", () => this.stop());
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) this.stop();
      else if (this.focused()) this.start();
    });
    input.addEventListener("keydown", event => {
      if (event.key === "F22" && event.isTrusted && this.acquiring && this.focused()) {
        this.challenged = true;
        event.preventDefault();
        return;
      }
      if (!this.owner || !event.isTrusted || !["F23", "F24"].includes(event.key)) return;
      event.preventDefault();
      this.lastMarker = performance.now();
      if (this.proxyMode && event.key === "F23") {
        this.proxyPending = true;
        this.setChinese(false);
        this.composing = true; // Prevent sending before the committed text arrives.
        this.refreshNow();
        return;
      }
      this.setChinese(event.key === "F24");
    });
    input.addEventListener("keydown", event => this.englishKey(event));
    input.addEventListener("compositionstart", () => {
      this.snapshot = { value: input.value, start: input.selectionStart, end: input.selectionEnd };
      this.composing = true;
      this.changed();
    });
    input.addEventListener("compositionend", () => {
      if (!this.chinese) this.cancelComposition();
      else { this.composing = false; this.snapshot = null; }
      this.changed();
    });
    input.addEventListener("input", event => {
      if (!this.chinese && this.snapshot && (event.isComposing ||
          (event.inputType || "").includes("Composition") || /\p{Script=Han}/u.test(event.data || ""))) {
        this.cancelComposition();
      } else if (!this.composing) this.snapshot = null;
    });
    input.addEventListener("beforeinput", event => {
      if (!this.chinese && ((event.inputType || "").includes("Composition") ||
          /\p{Script=Han}/u.test(event.data || ""))) {
        event.preventDefault();
      }
    });
    input.addEventListener("paste", event => {
      const text = event.clipboardData?.getData("text/plain") || "";
      if (!this.chinese && /\p{Script=Han}/u.test(text)) {
        event.preventDefault();
        this.hint.textContent = "按住 Ctrl 后再粘贴中文";
      } else if (this.fallback) {
        event.preventDefault();
        this.insert(text);
      }
    });
  }

  // Read-only Latin fallback keeps the composer usable when an embedded or
  // background browser cannot expose a native HWND. Read-only prevents the
  // system IME opening; only explicit Latin key events edit the DOM here.
  insert(text, start = this.input.selectionStart, end = this.input.selectionEnd) {
    this.undo.push({ value: this.input.value, start: this.input.selectionStart, end: this.input.selectionEnd });
    if (this.undo.length > 60) this.undo.shift();
    this.input.setRangeText(text, start, end, "end");
    this.input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
  }

  englishKey(event) {
    if (!this.fallback || this.chinese || event.defaultPrevented || event.isComposing || event.altKey || event.metaKey) return;
    if (event.ctrlKey) {
      if (event.key.toLowerCase() === "z" && !event.shiftKey && this.undo.length) {
        event.preventDefault();
        const previous = this.undo.pop();
        this.input.value = previous.value;
        this.input.setSelectionRange(previous.start, previous.end);
        this.input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "historyUndo" }));
      }
      return;
    }
    const start = this.input.selectionStart, end = this.input.selectionEnd;
    if (event.key === "Backspace" || event.key === "Delete") {
      event.preventDefault();
      const previous = Array.from(this.input.value.slice(0, start)).at(-1) || "";
      const next = Array.from(this.input.value.slice(end))[0] || "";
      this.insert("", start === end && event.key === "Backspace" ? start - previous.length : start,
        start === end && event.key === "Delete" ? end + next.length : end);
      return;
    }
    let text = event.key;
    if (/^Key[A-Z]$/.test(event.code || "")) {
      const upper = !!event.shiftKey !== !!event.getModifierState?.("CapsLock");
      text = upper ? event.code.slice(3) : event.code.slice(3).toLowerCase();
    } else {
      const punctuation = {
        Backquote: "`~", Minus: "-_", Equal: "=+", BracketLeft: "[{", BracketRight: "]}",
        Backslash: "\\|", Semicolon: ";:", Quote: "'\"", Comma: ",<", Period: ".>", Slash: "/?",
      };
      if (punctuation[event.code]) text = punctuation[event.code][event.shiftKey ? 1 : 0];
      if (/^Digit[0-9]$/.test(event.code || ""))
        text = event.shiftKey ? ")!@#$%^&*("[Number(event.code.slice(5))] : event.code.slice(5);
    }
    if (text === "Enter" && event.shiftKey) text = "\n";
    if (text.length === 1 && (text.charCodeAt(0) < 128 || text === "\n")) {
      event.preventDefault(); this.insert(text);
    }
  }

  focused() {
    return document.activeElement === this.input && document.hasFocus() && !document.hidden;
  }

  setTitle(title) {
    this.baseTitle = title;
    if (!this.acquiring) document.title = title;
  }

  cancelComposition() {
    if (this.snapshot) {
      this.input.value = this.snapshot.value;
      this.input.setSelectionRange(this.snapshot.start, this.snapshot.end);
    }
    this.composing = false;
    // Keep the snapshot until compositionend; late non-cancelable IME input
    // events must not reinsert an unconfirmed word after Ctrl has been released.
  }

  setChinese(value) {
    if (this.chinese === value) return;
    if (value && this.proxyMode && !this.proxyAnchor) {
      this.proxyAnchor = { start: this.input.selectionStart, end: this.input.selectionEnd };
    }
    this.chinese = value;
    if (!value) {
      const composing = this.composing;
      this.cancelComposition();
      if (composing) {
        this.input.readOnly = true;
        requestAnimationFrame(() => { if (this.owner && !this.proxyMode && this.focused()) this.input.readOnly = false; });
      }
    }
    this.hint.textContent = value ? "中文 · 松开 Ctrl 回填" : "英文 · Ctrl 中文";
    this.input.dataset.inputMode = value ? "chinese" : "english";
    this.changed();
  }

  async request(body, keepalive = false) {
    if (["confirm", "refresh"].includes(body.action)) body.bounds = this.nativeBounds();
    const response = await fetch("/api/input-mode", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), keepalive,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "input_unavailable");
    return data;
  }

  nativeBounds() {
    this.view?.resize();
    const box = this.input.getBoundingClientRect(), style = getComputedStyle(this.input);
    const caret = this.input.parentElement.querySelector(".input-caret");
    const row = this.input.parentElement.getBoundingClientRect();
    const left = Math.min(box.right - parseFloat(style.paddingRight) - 1,
      Math.max(box.left + parseFloat(style.paddingLeft), row.left + (parseFloat(caret?.style.left) || 0)));
    const top = Math.min(box.bottom - parseFloat(style.lineHeight),
      Math.max(box.top, row.top + (parseFloat(caret?.style.top) || 0)));
    const color = value => {
      const parts = value.match(/[\d.]+/g) || [0, 0, 0];
      return Number(parts[0]) | (Number(parts[1]) << 8) | (Number(parts[2]) << 16);
    };
    return { left, top, width: Math.max(1, box.right - left - parseFloat(style.paddingRight)),
      height: Math.min(parseFloat(style.lineHeight), box.bottom - top),
      viewport_width: window.innerWidth, viewport_height: window.innerHeight,
      font_size: parseFloat(style.fontSize), color: color(style.color),
      background: color(getComputedStyle(this.input.closest(".compose-box")).backgroundColor) };
  }

  release(owner) {
    if (owner) this.request({ action: "release", owner }, true).catch(() => {});
  }

  stop() {
    ++this.epoch;
    clearTimeout(this.timer); clearTimeout(this.watchdog);
    this.cancelComposition();
    this.setChinese(false);
    this.input.readOnly = true;
    this.fallback = true;
    this.proxyMode = false;
    this.proxyPending = false;
    this.committedSeen = "";
    this.proxyAnchor = null;
    const owner = this.owner; this.owner = 0;
    this.release(owner);
    this.hint.textContent = "英文 · 中文未连接";
    this.changed();
  }

  async start() {
    if (!this.focused() || this.acquiring || this.owner) return;
    const epoch = ++this.epoch;
    this.acquiring = true;
    this.challenged = false;
    this.input.readOnly = true;
    this.hint.textContent = "正在启用英文输入…";
    const marker = crypto.randomUUID().replaceAll("-", "");
    document.title = `EchoInput-${marker} · ${this.baseTitle}`;
    let pendingOwner = 0;
    try {
      if (epoch !== this.epoch || !this.focused()) return;
      let state = await this.request({ action: "acquire", marker });
      pendingOwner = state.owner;
      if (epoch !== this.epoch || !this.focused()) { this.release(state.owner); return; }
      for (let attempt = 0; attempt < 10 && !state.verified && !this.challenged; ++attempt)
        await new Promise(requestAnimationFrame);
      if ((!state.verified && !this.challenged) || epoch !== this.epoch || !this.focused()) {
        this.release(state.owner);
        throw new Error("input_dom_challenge_missing");
      }
      state = await this.request({ action: "confirm", owner: state.owner });
      if (epoch !== this.epoch || !this.focused()) { this.release(state.owner); return; }
      this.owner = state.owner;
      pendingOwner = 0;
      this.apply(state);
      this.pump(epoch);
    } catch (error) {
      this.release(pendingOwner);
      if (epoch === this.epoch) {
        if (this.lastFailure !== error.message) console.warn("Echo input mode:", error.message);
        this.lastFailure = error.message;
        this.hint.textContent = error.message === "input_release_modifiers" ? "英文输入 · 松开 Ctrl 后重试中文" :
          "英文输入可用 · 中文需原生焦点，请点击输入框或用独立浏览器";
        this.hint.title = this.hint.textContent;
        this.timer = setTimeout(() => this.start(), 1000);
      }
    } finally {
      this.acquiring = false;
      document.title = this.baseTitle;
      if (epoch !== this.epoch && this.focused()) this.start();
    }
  }

  apply(state, requestedAt = Infinity) {
    this.proxyMode = !!state.proxy;
    this.syncCommitted(state.committed || "");
    if (!state.active || !state.ready || state.failed) {
      this.stop();
      this.hint.textContent = state.failed ? "中文输入法未能启用，点击重试" : "英文 · 中文未连接";
      if (this.focused()) this.timer = setTimeout(() => this.start(), 300);
      return;
    }
    this.input.readOnly = this.proxyMode;
    this.fallback = this.proxyMode;
    if (this.lastMarker <= requestedAt) this.setChinese(state.chinese);
    if (this.proxyPending && !state.chinese && this.lastMarker <= requestedAt) {
      this.proxyPending = false;
      this.composing = false;
      this.changed();
    }
    if (this.proxyMode && !state.chinese && this.lastMarker <= requestedAt) this.proxyAnchor = null;
    if (!this.chinese) this.hint.textContent = "英文 · Ctrl 中文";
    clearTimeout(this.watchdog);
    this.watchdog = setTimeout(() => {
      this.stop();
      this.hint.textContent = "输入连接已中断，正在恢复…";
      if (this.focused()) this.timer = setTimeout(() => this.start(), 300);
    }, 850);
  }

  async pump(epoch) {
    if (epoch !== this.epoch || !this.owner || (!this.focused() && !this.proxyMode)) return;
    this.timer = setTimeout(async () => {
      try {
        const requestedAt = performance.now();
        const state = await this.request({ action: "refresh", owner: this.owner });
        if (epoch !== this.epoch) return;
        this.apply(state, requestedAt);
        this.pump(epoch);
      } catch (error) {
        if (epoch === this.epoch) this.stop();
      }
    }, 250);
  }

  syncCommitted(text) {
    // Cumulative, owner-scoped text survives lost/duplicate heartbeat responses.
    if (!text.startsWith(this.committedSeen) || text.length <= this.committedSeen.length) return;
    const delta = text.slice(this.committedSeen.length);
    this.committedSeen = text;
    const anchor = this.proxyAnchor || { start: this.input.selectionStart, end: this.input.selectionEnd };
    const selectedAnchor = this.input.selectionStart === anchor.start && this.input.selectionEnd === anchor.end;
    this.undo.push({ value: this.input.value, start: this.input.selectionStart, end: this.input.selectionEnd });
    if (this.undo.length > 60) this.undo.shift();
    this.input.setRangeText(delta, anchor.start, anchor.end, "preserve");
    if (selectedAnchor) this.input.setSelectionRange(anchor.start + delta.length, anchor.start + delta.length);
    this.proxyAnchor = null;
    this.input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: delta }));
  }

  async refreshNow() {
    const epoch = this.epoch, owner = this.owner, requestedAt = performance.now();
    if (!owner) return;
    try {
      const state = await this.request({ action: "refresh", owner });
      if (epoch === this.epoch && owner === this.owner) this.apply(state, requestedAt);
    } catch (error) { if (epoch === this.epoch) this.stop(); }
  }
}

// INPUT-007: serialize translation jobs; edits outside a job move its anchor,
// edits touching its Chinese invalidate it. Late results never overwrite edits.
class EchoInputTranslation {
  constructor(input, translate, context, changed, canApply = () => true) {
    Object.assign(this, { input, translate, context, changed, canApply });
    this.jobs = [];
    this.running = false;
    this.observed = input.value;
    input.addEventListener("input", () => this.track());
  }

  get pending() { return this.jobs.some(job => !job.cancelled); }
  get awaitingBoundary() { return /\p{Script=Han}$/u.test(this.input.value.slice(0, this.input.selectionStart)); }

  deferWordBoundary(insertSpace) { this.wordBoundary ||= insertSpace; }

  flushWordBoundary() {
    if (!this.wordBoundary || !this.canApply()) return;
    const insertSpace = this.wordBoundary;
    this.wordBoundary = null;
    if (!this.confirmWord(insertSpace)) insertSpace();
  }

  // INPUT-010: only plain Space confirms a complete Chinese lexical item.
  // Read the entire contiguous word before the caret, even if it was entered
  // through several Ctrl sessions. The IME's own confirmation Space never lands here.
  confirmWord(insertSpace) {
    const end = this.input.selectionStart;
    if (end !== this.input.selectionEnd) return false;
    const word = this.input.value.slice(0, end).match(/\p{Script=Han}+$/u)?.[0];
    if (!word) return false;
    insertSpace(); // retain the user's delimiter on success, abstention or failure
    this.enqueue({ start: end - word.length, end });
    return true;
  }

  track() {
    const old = this.observed, value = this.input.value;
    if (old === value) return;
    let start = 0, end = old.length, nextEnd = value.length;
    while (start < end && start < nextEnd && old[start] === value[start]) ++start;
    while (end > start && nextEnd > start && old[end - 1] === value[nextEnd - 1]) { --end; --nextEnd; }
    for (const job of this.jobs) {
      if (job.end <= start) continue;
      if (job.start >= end) {
        job.start += nextEnd - end; job.end += nextEnd - end;
      } else job.cancelled = true;
    }
    this.observed = value;
  }

  enqueue(span) {
    this.track();
    const text = this.input.value.slice(span.start, span.end);
    if (!/\p{Script=Han}/u.test(text)) return;
    this.jobs.push({ start: span.start, end: span.end, text, context: this.context() });
    this.changed();
    this.pump();
  }

  async pump() {
    if (this.running) return;
    this.running = true;
    try {
      while (this.jobs.length) {
        // A second Ctrl session owns a native insertion anchor. Do not move
        // its surrounding DOM until the final committed snapshot has arrived.
        if (!this.canApply()) break;
        this.track();
        const job = this.jobs[0];
        let deferred = false;
        try {
          if (job.cancelled || job.context !== this.context()) continue;
          const result = job.result || await this.translate({ chinese: job.text,
            before: this.input.value.slice(0, job.start), after: this.input.value.slice(job.end) });
          if (!this.canApply()) { job.result = result; deferred = true; break; }
          this.track();
          if (job.cancelled || job.context !== this.context() ||
              this.input.value.slice(job.start, job.end) !== job.text) continue;
          if (!result.word) { this.changed(result.reason || "unresolved"); continue; }
          let word = result.word;
          const before = this.input.value.slice(0, job.start), after = this.input.value.slice(job.end);
          if (!before.trim() || /[.!?]\s*$/.test(before)) word = word[0].toUpperCase() + word.slice(1);
          if (/[A-Za-z']$/.test(before)) word = " " + word;
          if (!after || /^[A-Za-z']/i.test(after)) word += " ";
          const start = this.input.selectionStart, end = this.input.selectionEnd, direction = this.input.selectionDirection;
          this.jobs.shift(); // the replacement is an edit only for subsequent jobs
          this.input.setRangeText(word, job.start, job.end, "preserve");
          const position = p => p < job.start ? p : p >= job.end ? p + word.length - job.text.length : job.start + word.length;
          this.input.setSelectionRange(position(start), position(end), direction);
          this.input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertReplacementText", data: word }));
        } catch (error) {
          this.changed("translation_unavailable");
        } finally {
          if (!deferred && this.jobs[0] === job) this.jobs.shift();
          this.changed();
        }
      }
    } finally { this.running = false; }
  }
}
