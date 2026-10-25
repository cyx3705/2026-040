/* YT-002/003/004/005/006/008: alternating current/history slots, held through gaps, bounded
   ahead-of-playback requests to the existing shared-KV reader endpoint. */
(() => {
  if (globalThis.__echoYouTube) return;
  const CORE = globalThis.EchoYouTubeTimeline;
  let enabled = false, ready = false, leadFrames = 12, fps = 30, generation = 0, serial = 0;
  let videoId = '', cues = [], track = '', source = 'waiting', lastError = '', translationError = '', port;
  let player, media, frame, lastFrame, tickTimer, probeTimer, learnedTimer;
  let current = null, host, shadow, nativeStyle;
  let slots = [], activeSlot = -1, lastCue = '', lastIndex = -1, lastTime = null;
  let liveOccurrence = 0;
  let blockedNative = '', liveSignature = '', liveChanged = 0;
  const cache = new Map(), requests = new Map(), saving = new Set();
  let learned = new Set(), reconnectTimer, learnedRevision = 0;
  // YT-009: finished glosses are kept per video in extension storage and written
  // before the page goes away, so reopening a video resumes instead of recomputing.
  const STORE = 'echoYouTubeGlosses:', STORE_INDEX = 'echoYouTubeGlossIndex', STORE_VIDEOS = 60;
  const ITEM_KEYS = ['start', 'end', 'word', 'lemma', 'gloss', 'state', 'fallback_reason'];
  let saved = {id: '', texts: {}, loading: null}, saveTimer, dirty = false, savedIndex = [];
  const idNow = () => {
    const url = new URL(location.href);
    return (url.pathname === '/watch' ? url.searchParams.get('v') : url.pathname.match(/^\/shorts\/([^/]+)/)?.[1]) || '';
  };
  const bridge = kind => window.postMessage({source: 'echo-youtube-extension', kind}, location.origin);
  async function rpc(type, values = {}) {
    const result = await chrome.runtime.sendMessage({type, ...values});
    if (result.error) throw new Error(result.error);
    return result.data;
  }
  function nativeCaption() {
    return CORE.clean([...(player?.querySelectorAll('.ytp-caption-window-container .ytp-caption-segment') || [])]
      .filter(node => node.getClientRects().length && (player.hasAttribute('data-echo-youtube-active') ||
        getComputedStyle(node).visibility !== 'hidden')).map(node => node.textContent).join(' '));
  }
  function hide() {
    if (host) host.hidden = true;
    player?.removeAttribute('data-echo-youtube-active'); current = null;
  }
  function resetDisplay() {
    hide(); activeSlot = -1; lastCue = ''; lastIndex = -1; lastTime = null;
    for (const slot of slots) {
      slot.entry = null; slot.nodes.clear(); slot.line.replaceChildren();
      slot.line.dataset.role = 'empty'; slot.line.removeAttribute('aria-current');
    }
  }
  function seek() {
    resetDisplay(); tick();
  }
  function cancel(entry) {
    if (!entry.id) return;
    try { port?.postMessage({type: 'cancel', id: entry.id}); } catch { connectionLost(port); }
    requests.delete(entry.id); entry.id = null;
  }
  function adopt(entry, items) {
    entry.items = new Map(items.map(item => [item.start, {...item}]));
    entry.done = entry.complete = entry.dictionary = true; entry.retries = 0;
    // A word saved as learned but since restored needs its gloss computed again.
    if (ready && stale(entry)) entry.done = false;
  }
  const stale = entry => [...entry.items.values()].some(item => item.state === 'skipped_learned' && !learned.has(item.lemma));
  function restore(id) {
    const state = saved = {id, texts: {}, loading: null};
    if (!id) return;
    state.loading = chrome.storage.local.get([STORE + id, STORE_INDEX]).then(result => {
      if (saved !== state) return;
      // Other tabs may have saved since; keep their order and put ours in front on the next write.
      savedIndex = [...new Set([...savedIndex, ...(result[STORE_INDEX] || [])])];
      const texts = result[STORE + id]?.texts || {};
      state.texts = {...texts, ...state.texts}; // This session's newer results win.
      for (const [text, items] of Object.entries(texts)) {
        const entry = cache.get(text);
        if (entry && !entry.done && Array.isArray(items)) { cancel(entry); adopt(entry, items); paint(entry); }
      }
    }).catch(() => {}).finally(() => { if (saved === state) { state.loading = null; if (dirty) schedule(); tick(); } });
  }
  function remember(entry) {
    if (!saved.id) return;
    saved.texts[entry.text] = [...entry.items.values()].map(item => Object.fromEntries(ITEM_KEYS.filter(key => item[key] != null).map(key => [key, item[key]])));
    dirty = true; schedule();
  }
  function schedule() {
    if (!saveTimer) saveTimer = setTimeout(flush, 1500);
  }
  function flush() {
    clearTimeout(saveTimer); saveTimer = null;
    const {id, texts, loading} = saved;
    // Writing before the stored copy is merged would drop earlier sessions' work.
    if (!dirty || !id || loading) return;
    dirty = false;
    // One synchronous write for record and index: on pagehide there is no later turn.
    const index = [id, ...savedIndex.filter(value => value !== id)];
    savedIndex = index.slice(0, STORE_VIDEOS);
    void chrome.storage.local.set({[STORE + id]: {at: Date.now(), texts}, [STORE_INDEX]: savedIndex}).catch(() => {});
    if (index.length > STORE_VIDEOS) void chrome.storage.local.remove(index.slice(STORE_VIDEOS).map(value => STORE + value)).catch(() => {});
  }
  function clearCache() {
    for (const entry of cache.values()) cancel(entry);
    cache.clear(); resetDisplay();
  }
  function overlay() {
    if (!player || host?.parentElement === player) return;
    host?.remove(); player.removeAttribute('data-echo-youtube-active');
    host = document.createElement('div'); host.dataset.echoYoutube = ''; host.hidden = true;
    shadow = host.attachShadow({mode: 'open'});
    const style = document.createElement('style');
    style.textContent = `
      :host{position:absolute;left:4%;bottom:14%;width:92%;z-index:60;pointer-events:none;color:#fff;font:clamp(14px,1.65vw,24px)/1.35 system-ui,sans-serif;text-shadow:0 1px 3px #000}
      :host([hidden]){display:none!important}
      .panel{display:grid;grid-template-rows:1fr 1fr;gap:.2em;max-height:50vh;overflow:hidden}
      .line{display:flex;flex-wrap:wrap;justify-content:center;align-content:center;align-items:start;column-gap:.12em;row-gap:.15em;min-height:2.55em;background:rgba(0,0,0,.76);border-radius:5px;padding:.3em .4em}
      .line[data-role="current"]{box-shadow:inset 3px 0 #dcebe6}
      .token{position:relative;display:inline-grid;grid-template-rows:1.35em 1.2em;grid-template-columns:minmax(3.6em,max-content);padding:0;border:0;background:none;color:inherit;font:inherit;text-shadow:inherit;pointer-events:auto;cursor:pointer}
      .token:disabled{cursor:default}.token:hover:not(:disabled){background:rgba(255,255,255,.12)}
      .en{grid-row:1;white-space:pre}.zh{position:absolute;bottom:0;left:0;width:100%;height:1.7em;font:.65em/1.7 system-ui,sans-serif;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#dcebe6}
      .token[data-degraded] .zh{text-decoration:underline wavy currentColor;text-underline-offset:2px}
      .punct{white-space:pre;line-height:1.35}
    `;
    const panel = document.createElement('div'); panel.className = 'panel';
    slots = [0, 1].map(index => {
      const line = document.createElement('div'); line.className = 'line';
      line.dataset.slot = index; line.dataset.role = 'empty'; panel.append(line);
      return {line, entry: null, nodes: new Map()};
    });
    activeSlot = -1; lastCue = ''; shadow.append(style, panel);
    for (const name of ['click', 'dblclick', 'mousedown']) host.addEventListener(name, event => event.stopPropagation());
    player.append(host);
    if (!nativeStyle) {
      nativeStyle = document.createElement('style'); nativeStyle.dataset.echoYoutube = '';
      nativeStyle.textContent = '.html5-video-player[data-echo-youtube-active] .ytp-caption-window-container{visibility:hidden!important}';
      document.documentElement.append(nativeStyle);
    }
  }
  function paint(entry) {
    if (!host) return;
    for (const slot of slots.filter(slot => slot.entry === entry)) for (const [start, button] of slot.nodes) {
      const item = entry.items.get(start);
      if (!item) continue;
      const known = learned.has(item.lemma) || item.state === 'skipped_learned';
      button.querySelector('.zh').textContent = known ? '' : item.gloss || '';
      button.disabled = !entry.dictionary || item.state === 'missing';
      button.dataset.lemma = item.lemma;
      button.toggleAttribute('data-known', known);
      button.toggleAttribute('data-degraded', item.state === 'degraded' && !known);
      button.title = known ? '已学会 · 点击恢复中文' :
        `${item.gloss || ''}${item.state === 'pending' ? ' · 正在选义' : ''}${item.fallback_reason ? ` · 默认释义：${item.fallback_reason}` : ''} · 点击标熟`;
    }
  }
  function show(entry, cueKey) {
    overlay();
    if (!host) return;
    if (lastCue !== cueKey) {
      activeSlot = activeSlot < 0 ? 1 : 1 - activeSlot; lastCue = cueKey;
      const slot = slots[activeSlot], line = slot.line;
      slot.entry = entry; line.replaceChildren(); slot.nodes.clear(); let cursor = 0;
      for (const item of CORE.words(entry.text)) {
        const punctuation = document.createElement('span'); punctuation.className = 'punct';
        punctuation.textContent = entry.text.slice(cursor, item.start); line.append(punctuation);
        const button = document.createElement('button'); button.type = 'button'; button.className = 'token';
        const en = document.createElement('span'); en.className = 'en'; en.textContent = item.word;
        const zh = document.createElement('span'); zh.className = 'zh'; zh.setAttribute('aria-hidden', 'true');
        button.append(en, zh); button.disabled = true;
        button.onclick = async event => {
          event.stopPropagation();
          const lemma = button.dataset.lemma, value = !learned.has(lemma), version = generation;
          if (saving.has(lemma)) return;
          saving.add(lemma); learnedRevision++;
          try {
            await rpc('set-learned', {lemma, learned: value});
            if (enabled && version === generation) learnedChange(lemma, value);
          } catch (error) { lastError = error.message; button.title = `保存失败：${lastError}`; }
          finally { saving.delete(lemma); }
        };
        slot.nodes.set(item.start, button); line.append(button); cursor = item.end;
      }
      const tail = document.createElement('span'); tail.className = 'punct'; tail.textContent = entry.text.slice(cursor); line.append(tail);
      for (const [index, view] of slots.entries()) {
        view.line.dataset.role = index === activeSlot ? 'current' : view.entry ? 'history' : 'empty';
        if (index === activeSlot) view.line.setAttribute('aria-current', 'true'); else view.line.removeAttribute('aria-current');
      }
    }
    current = entry;
    host.hidden = false; player.setAttribute('data-echo-youtube-active', ''); paint(entry);
  }
  function entryFor(cue) {
    let entry = cache.get(cue.text);
    if (!entry) {
      entry = {text: cue.text, items: new Map(CORE.words(cue.text).map(item => [item.start, item])),
        id: null, done: false, dictionary: false};
      if (Array.isArray(saved.texts[cue.text])) adopt(entry, saved.texts[cue.text]);
      cache.set(cue.text, entry);
    }
    cache.delete(cue.text); cache.set(cue.text, entry); return entry;
  }
  function failPending(entry, reason) {
    for (const item of entry.items.values()) if (item.state === 'pending') {
      item.state = 'degraded'; item.fallback_reason = reason;
    }
  }
  function learnedChange(lemma, value) {
    learnedRevision++;
    const changed = learned.has(lemma) !== value;
    if (value) learned.add(lemma); else learned.delete(lemma);
    for (const entry of cache.values()) {
      if (changed && !value && [...entry.items.values()].some(item => item.lemma === lemma && item.state === 'skipped_learned')) {
        cancel(entry); entry.done = false;
      }
      paint(entry);
    }
    tick();
  }
  async function syncLearned() {
    const version = generation, revision = learnedRevision;
    try {
      // YT-008: use the same worker protocol as web reading, including during upgrades.
      const data = await rpc('learned');
      if (!enabled || version !== generation || revision !== learnedRevision) return;
      if (!port) connect();
      const next = new Set(data.map(item => item.lemma));
      for (const lemma of learned) if (!next.has(lemma)) learnedChange(lemma, false);
      learned = next; ready = true;
      translationError = '';
      for (const entry of cache.values()) if (entry.done && !entry.id && stale(entry)) entry.done = false;
      for (const entry of cache.values()) paint(entry);
      tick();
    } catch (error) { if (version === generation) translationError = error.message; }
  }
  function connectionLost(active) {
    if (!active || port !== active) return;
    port = null; ready = false; translationError = '伴读连接正在恢复';
    active.disconnect(); clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => { if (enabled && !document.hidden) void syncLearned(); }, 3000);
    for (const entry of requests.values()) {
      failPending(entry, 'connection_error'); entry.id = null; entry.done = false; paint(entry);
    }
    requests.clear();
  }
  function connect() {
    const active = chrome.runtime.connect({name: 'echo-reader'}); port = active;
    active.onMessage.addListener(message => {
      if (active !== port) return;
      if (message.learned) { learnedChange(message.learned.lemma, message.learned.value); return; }
      const entry = requests.get(message.id);
      if (!entry) return;
      if (message.items) {
        if (message.phase === 'dictionary') entry.dictionary = true;
        for (const item of message.items) {
          const previous = entry.items.get(item.start);
          entry.items.set(item.start, item.state === 'skipped_learned' && previous?.gloss ? previous : item);
        }
      }
      if (message.done) entry.complete = true;
      if (message.error) { translationError = message.error; entry.complete = false; failPending(entry, 'connection_error'); }
      if (message.finished) {
        failPending(entry, 'stream_interrupted'); entry.done = Boolean(entry.complete);
        // Back off instead of giving up: a busy or reloading host recovers on its own.
        entry.retries = entry.done ? 0 : (entry.retries || 0) + 1;
        entry.retryAt = Date.now() + Math.min(30000, 3000 * 2 ** Math.max(0, entry.retries - 1));
        if (entry.done) { translationError = ''; remember(entry); }
        entry.id = null; requests.delete(message.id);
      }
      paint(entry);
    });
    active.onDisconnect.addListener(() => connectionLost(active));
  }
  function bindMedia() {
    const next = player?.querySelector('video');
    if (next === media) return;
    if (frame !== undefined) media?.cancelVideoFrameCallback?.(frame);
    media?.removeEventListener('seeking', seek);
    media?.removeEventListener('ended', tick);
    resetDisplay();
    media = next; lastFrame = null; fps = 30;
    media?.addEventListener('seeking', seek);
    media?.addEventListener('ended', tick);
    const callback = (now, data) => {
      if (!enabled || next !== media) return;
      if (lastFrame && data.mediaTime > lastFrame.mediaTime) {
        const estimate = (data.presentedFrames - lastFrame.presentedFrames) / (data.mediaTime - lastFrame.mediaTime);
        if (estimate >= 15 && estimate <= 120) fps = estimate;
      }
      lastFrame = data; tick(); frame = media.requestVideoFrameCallback(callback);
    };
    if (media?.requestVideoFrameCallback) frame = media.requestVideoFrameCallback(callback);
  }
  function acceptTimeline(next, key) {
    if (!next.length) return;
    if (track !== key) clearCache();
    cues = next; track = key; source = 'timeline'; lastError = ''; tick();
  }
  function probe() {
    if (!enabled || !idNow()) return;
    if (!cues.length) bridge('request-timeline');
    for (const native of [...(media?.textTracks || [])]) {
      if (!/^en(?:-|$)/i.test(native.language || '') || !native.cues?.length) continue;
      acceptTimeline(CORE.normalize([...native.cues].map(cue => ({start: cue.startTime, end: cue.endTime,
        text: cue.getCueAsHTML?.().textContent || cue.text}))), `native:${native.language}:${native.label}`);
      break;
    }
  }
  function tick() {
    if (!enabled) return;
    const nextId = idNow();
    if (nextId !== videoId) {
      blockedNative = videoId ? nativeCaption() : ''; liveSignature = ''; liveChanged = 0;
      flush(); clearCache(); cues = []; track = ''; source = 'waiting'; videoId = nextId; restore(videoId); bridge('stop');
      if (videoId) bridge('request-timeline');
    }
    const nextPlayer = videoId && document.querySelector('#movie_player, .html5-video-player');
    if (player !== nextPlayer) {
      resetDisplay(); player?.removeAttribute('data-echo-youtube-active'); host?.remove(); host = null;
      slots = []; player = nextPlayer;
    }
    bindMedia();
    if (!player) { hide(); return; }
    if (player.matches('.ad-showing,.ad-interrupting')) {
      for (const entry of cache.values()) cancel(entry);
      source = 'ad'; lastTime = null; hide(); return;
    }
    if (media?.ended) {
      for (const entry of cache.values()) cancel(entry);
      hide(); return;
    }
    if (cues.length) source = 'timeline';
    const time = media?.currentTime || 0;
    const plan = cues.length ? CORE.plan(cues, time, leadFrames / fps) : null;
    const index = plan?.activeIndex ?? -1;
    // A seek/non-adjacent cue must not keep unrelated or future text as history.
    if ((lastTime !== null && time < lastTime - .05) ||
        (index >= 0 && lastIndex >= 0 && index !== lastIndex && index !== lastIndex + 1)) resetDisplay();
    lastTime = time;
    if (index >= 0) lastIndex = index;
    let text = plan ? plan.active?.text : nativeCaption();
    if (!plan && blockedNative) { if (text === blockedNative) text = ''; else blockedNative = ''; }
    if (!plan && text !== liveSignature) { liveSignature = text; liveChanged = performance.now(); liveOccurrence++; }
    if (!cues.length) source = (text || slots[activeSlot]?.entry) ? 'live' : 'waiting';
    const wantedCues = plan?.wanted || (text ? [{text}] : []);
    const upcoming = wantedCues.filter(cue => cue.text.length <= 1600 && CORE.words(cue.text).length).map(entryFor);
    const active = text && upcoming.find(entry => entry.text === text);
    const held = !plan && slots[activeSlot]?.entry;
    if (active) show(active, plan ? `timeline:${index}:${plan.active.start}:${plan.active.end}:${text}` : `live:${liveOccurrence}`);
    else if (held) show(held, lastCue);
    else hide();
    // Finish a delayed history result, while preserving the three-task budget.
    const visible = slots.map(slot => slot.entry).filter(Boolean);
    const wanted = [...new Set([...(current ? [current] : []), ...visible.filter(entry => !entry.done), ...upcoming])].slice(0, 3);
    for (const entry of cache.values()) if (entry.id && !wanted.includes(entry)) cancel(entry);
    if (ready && port && (plan || performance.now() - liveChanged >= 120)) for (const entry of wanted) if (!entry.done && !entry.id && Date.now() >= (entry.retryAt || 0)) {
      entry.complete = false; entry.id = `${generation}:${++serial}`; requests.set(entry.id, entry);
      try { port.postMessage({type: 'gloss', id: entry.id, text: entry.text, priority: entry === current, session: `yt:${videoId}`}); }
      catch { connectionLost(port); break; }
    }
    for (const [key, entry] of cache) {
      if (cache.size <= 64) break;
      if (!entry.id && !wanted.includes(entry) && !visible.includes(entry)) cache.delete(key);
    }
  }
  function stop() {
    enabled = false; ready = false; generation++;
    clearInterval(tickTimer); clearInterval(probeTimer); clearInterval(learnedTimer); clearTimeout(reconnectTimer);
    if (frame !== undefined) media?.cancelVideoFrameCallback?.(frame);
    media?.removeEventListener('seeking', seek);
    media?.removeEventListener('ended', tick);
    flush(); clearCache(); port?.disconnect(); port = null; host?.remove(); host = null;
    slots = []; nativeStyle?.remove(); nativeStyle = null; media = null; cues = []; track = ''; videoId = ''; bridge('stop');
  }
  function start() {
    if (enabled) return;
    enabled = true; ready = false; generation++; lastError = ''; translationError = ''; connect();
    tick(); probe(); void syncLearned();
    tickTimer = setInterval(tick, 50); probeTimer = setInterval(probe, 2000); learnedTimer = setInterval(syncLearned, 15000);
  }
  function snapshot() {
    return {enabled, video: Boolean(idNow()), caption: current?.text || (cues.length || media?.ended ? '' : nativeCaption()), source,
      leadFrames, fps: Math.round(fps), error: translationError || lastError, cached: cache.size, pending: requests.size,
      saved: Object.keys(saved.texts).length};
  }
  window.addEventListener('message', event => {
    const data = event.data;
    if (!enabled || event.source !== window || event.origin !== location.origin || data?.source !== 'echo-youtube-page') return;
    // The page world may report the new video before the next tick notices the URL change.
    if (data.videoId !== videoId && data.videoId === idNow()) tick();
    if (data.videoId !== videoId) return;
    if (data.kind === 'timeline' && data.language === 'en') {
      try { acceptTimeline(CORE.parse(data.body), String(data.track).slice(0, 200)); }
      catch { lastError = '字幕时间轴无法解析，使用实时字幕'; }
    } else if (data.kind === 'unavailable' && !cues.length) lastError = String(data.reason).slice(0, 200);
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes.connection && enabled) { stop(); start(); }
    if (changes.leadFrames) leadFrames = Math.max(1, Math.min(120, Number(changes.leadFrames.newValue) || 12));
    if (changes.enabled) { if (changes.enabled.newValue === false) stop(); else start(); }
    tick();
  });
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.type === 'echo-youtube-state') reply(snapshot());
    if (message.type === 'echo-youtube-retry') { if (enabled) { stop(); start(); } reply(snapshot()); }
  });
  const wake = () => {
    if (!enabled || document.hidden) return;
    for (const entry of cache.values()) if (!entry.done) { entry.retries = 0; entry.retryAt = 0; }
    void syncLearned(); probe();
  };
  document.addEventListener('visibilitychange', () => { if (document.hidden) flush(); else wake(); });
  window.addEventListener('pagehide', flush);
  window.addEventListener('pageshow', wake);
  window.addEventListener('online', wake);
  globalThis.__echoYouTube = {snapshot, start, stop};
  chrome.storage.local.get({enabled: true, leadFrames: 12}).then(settings => {
    leadFrames = Math.max(1, Math.min(120, Number(settings.leadFrames) || 12));
    if (settings.enabled) start();
  });
})();
