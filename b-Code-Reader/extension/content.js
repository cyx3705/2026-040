/* WEB-003: isolated-world, visible blocks only; no page HTML sent or executed. */
(() => {
  if (globalThis.__echoReader) return;
  if (!/^https?:$/.test(location.protocol) || ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) return;
  const CORE = globalThis.EchoReaderCore;
  const BLOCK = 'p,li,dt,dd,figcaption,td,th,h1,h2,h3,h4,h5,h6,blockquote,div,section,article,main,body';
  const SKIP = 'script,style,noscript,textarea,input,select,option,button,pre,code,svg,math,canvas,iframe,ruby,.html5-video-player,[data-echo-youtube],[contenteditable]:not([contenteditable="false"]),[role="textbox"],[data-echo-word],[data-echo-ui]';
  let enabled = false, port, watcher, visible, current, serial = 0, epoch = 0, lastError = '';
  let queue = [], seen = new WeakSet(), scheduled = new WeakSet(), learned = new Set();
  const records = new Set();
  let learnedRevision = 0;
  const saving = new Set();
  let syncTimer, scanTimer, retryTimer, paused = false;
  const interrupted = new Set();
  const style = document.createElement('style');
  style.dataset.echoUi = '';
  style.textContent = `
    [data-echo-word]{display:inline-flex!important;flex-direction:column!important;align-items:center!important;vertical-align:baseline!important;white-space:nowrap!important;line-height:1.25!important;font:inherit;color:inherit!important;--echo-reader-foreground:currentColor!important;cursor:pointer}
    [data-echo-word]>.echo-reader-en{font:inherit!important;color:inherit!important}
    /* WEB-006: currentColor here is the inherited page foreground, including
       links and live theme changes. The inherited variable avoids Chromium's
       stale color-mix(currentColor) style cache after a parent theme change. */
    [data-echo-word]>.echo-reader-zh{display:block!important;font:normal max(10px,.65em)/1.35 system-ui,sans-serif!important;color:inherit!important;color:color-mix(in srgb,var(--echo-reader-foreground) 92%,#588c80)!important;max-width:9em!important;overflow:hidden!important;text-overflow:ellipsis!important}
    [data-echo-word][data-known]>.echo-reader-zh{display:none!important}
    [data-echo-word][data-degraded]>.echo-reader-zh{text-decoration:underline wavy currentColor!important;text-underline-offset:2px!important}
    [data-echo-word]:hover{background:color-mix(in srgb,currentColor 8%,transparent)!important;border-radius:2px!important}
    @media(forced-colors:active){[data-echo-word]>.echo-reader-zh{color:inherit!important}}
  `;
  function allowed(node) {
    return node.nodeType === Node.TEXT_NODE &&
      node.parentElement && !node.parentElement.closest(SKIP) &&
      !node.parentElement.closest('[hidden],[aria-hidden="true"]');
  }
  function eligible(node) { return allowed(node) && /[A-Za-z]{2}/.test(node.data); }
  function keptText(text) { const node = document.createTextNode(text); seen.add(node); return node; }
  function textNodes(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) if (allowed(walker.currentNode)) nodes.push(walker.currentNode);
    return nodes;
  }
  function discover(root = document.body) {
    if (!enabled || !root?.isConnected) return;
    const candidates = root.nodeType === Node.TEXT_NODE ? [root] : textNodes(root);
    for (const node of candidates) {
      if (!eligible(node) || seen.has(node)) continue;
      const block = node.parentElement.closest(BLOCK) || node.parentElement;
      if (!scheduled.has(block)) { scheduled.add(block); visible.observe(block); }
    }
  }
  function collect(block) {
    if (!enabled || !block.isConnected || block.closest(SKIP)) return;
    const rect = block.getBoundingClientRect();
    if (!rect.width || !rect.height || getComputedStyle(block).visibility === 'hidden') return;
    // Direct block ownership avoids retranslating text in nested paragraphs.
    const nodes = textNodes(block).filter(node => !seen.has(node) &&
      (node.parentElement.closest(BLOCK) || node.parentElement) === block);
    if (!nodes.length) return;
    let text = '', parts = [];
    for (const node of nodes) {
      parts.push({node, start: text.length, end: text.length + node.length}); text += node.data;
    }
    const chunks = CORE.chunks(text);
    // Split text at all chunk edges before wrapping any chunk, preserving live
    // node identities for later chunks in the same paragraph.
    const boundaries = chunks.slice(0, -1).map(c => c.end);
    const splitParts = [];
    for (const part of parts) {
      let node = part.node, start = part.start;
      for (const boundary of boundaries.filter(b => b > part.start && b < part.end)) {
        const tail = node.splitText(boundary - start);
        splitParts.push({node, start, end: boundary}); seen.add(node);
        node = tail; start = boundary;
      }
      splitParts.push({node, start, end: part.end}); seen.add(node);
    }
    for (const chunk of chunks) {
      if (!/[A-Za-z]{2}/.test(chunk.text)) continue;
      if (chunk.text.length > 1600) continue;
      queue.push({id: ++serial, chunk, parts: splitParts.filter(p => p.start < chunk.end && p.end > chunk.start),
                  records: new Map(), epoch});
    }
    pump();
  }
  function paint(record, item = record.item) {
    if (item.state === 'skipped_learned' && record.saved) item = record.saved;
    if (item.gloss && ['normal', 'degraded'].includes(item.state)) record.saved = item;
    record.item = item;
    const known = learned.has(item.lemma);
    for (const element of record.elements) {
      element.toggleAttribute('data-known', known);
      element.toggleAttribute('data-pending', item.state === 'pending' && !known);
      element.toggleAttribute('data-degraded', item.state === 'degraded');
      element.title = known ? '已学会 · 点击恢复中文（链接内 Alt＋点击）' :
        `${item.gloss || ''}${item.state === 'pending' ? ' · 正在按语境选义' : ''}${item.fallback_reason ? ` · 默认释义：${item.fallback_reason}` : ''} · 点击标熟（链接内 Alt＋点击）`;
    }
    if (record.zh) record.zh.textContent = known ? '' : item.gloss || '';
  }
  function render(job, items) {
    if (job.epoch !== epoch) return;
    // Assemble replacements per original node, in one pass. No innerHTML from
    // either the website, dictionary or model is ever inserted.
    const edits = new Map();
    for (const item of items) {
      if (item.state === 'missing') continue;
      const record = {item, elements: [], zh: null, job};
      job.records.set(item.start, record); records.add(record);
      const ranges = CORE.ranges(job.parts, job.chunk, item);
      for (let i = 0; i < ranges.length; i++) {
        const range = ranges[i];
        if (!range.node.isConnected) continue;
        if (!edits.has(range.node)) edits.set(range.node, []);
        edits.get(range.node).push({...range, record, first: i === 0});
      }
    }
    for (const [node, ranges] of edits) {
      if (!node.isConnected) continue;
      const expected = job.chunk.text.slice(Math.max(0, job.parts.find(p => p.node === node).start - job.chunk.start),
        job.parts.find(p => p.node === node).end - job.chunk.start);
      if (node.data !== expected) continue; // Website changed while queued.
      const fragment = document.createDocumentFragment(); let cursor = 0;
      for (const range of ranges.sort((a, b) => a.start - b.start)) {
        if (range.start < cursor) continue;
        fragment.append(keptText(node.data.slice(cursor, range.start)));
        const wrapper = document.createElement('span'); wrapper.dataset.echoWord = range.record.item.lemma;
        const en = document.createElement('span'); en.className = 'echo-reader-en';
        en.textContent = node.data.slice(range.start, range.end); wrapper.append(en);
        if (range.first) {
          const zh = document.createElement('span'); zh.className = 'echo-reader-zh';
          zh.setAttribute('aria-hidden', 'true'); wrapper.append(zh); range.record.zh = zh;
        }
        range.record.elements.push(wrapper); fragment.append(wrapper); cursor = range.end;
      }
      fragment.append(keptText(node.data.slice(cursor))); node.replaceWith(fragment);
    }
    for (const record of job.records.values()) paint(record);
  }
  function failPending(job, reason) {
    for (const record of job.records.values()) if (record.item.state === 'pending') {
      paint(record, {...record.item, state: 'degraded', fallback_reason: reason});
    }
  }
  function pump() {
    if (!enabled || current || !port || paused) return;
    queue = queue.filter(liveJob);
    const index = queue.findIndex(job => [...job.parts.map(part => part.node),
      ...[...job.records.values()].flatMap(record => record.elements)].some(node => {
      if (!node.isConnected) return false;
      const range = document.createRange(); range.selectNodeContents(node);
      const rect = range.getBoundingClientRect();
      return rect.width > 0 && rect.bottom >= -200 && rect.top <= innerHeight + 200;
    }));
    if (index < 0) return;
    current = queue.splice(index, 1)[0];
    current.complete = false; current.failed = false;
    // KV-001: one page is one context; the host keeps its KV across visits.
    try { port.postMessage({type: 'gloss', id: current.id, text: current.chunk.text, session: `web:${location.origin}${location.pathname}`}); }
    catch { connectionLost(port); }
  }
  function scroll() { clearTimeout(scanTimer); scanTimer = setTimeout(pump, 80); }
  async function syncLearned() {
    const version = epoch, revision = learnedRevision;
    try {
      const result = await chrome.runtime.sendMessage({type: 'learned'});
      if (result.error) throw new Error(result.error);
      if (!enabled || version !== epoch) return;
      if (revision !== learnedRevision) return true;
      const next = new Set(result.data.map(item => item.lemma));
      learned = next;
      refreshSkipped();
      for (const record of records) {
        if (!record.elements.some(element => element.isConnected)) records.delete(record);
        else paint(record);
      }
      return true;
    } catch (error) { lastError = error.message; return false; }
  }
  // WEB-008: update membership in place. Only a never-translated occurrence
  // needs its original chunk again; cached glosses and other chunks stay intact.
  function refreshSkipped() {
    const groups = new Map();
    for (const record of records) {
      if (learned.has(record.item.lemma) || record.item.state !== 'skipped_learned' ||
          record.saved || record.refreshQueued || !record.elements.some(e => e.isConnected)) continue;
      record.refreshQueued = true;
      if (!groups.has(record.job)) groups.set(record.job, new Map());
      groups.get(record.job).set(record.item.start, record);
    }
    for (const [source, items] of groups) queue.push({id: ++serial, chunk: source.chunk,
      parts: [], records: items, epoch});
    pump();
  }
  function learnedChange(lemma, value) {
    learnedRevision++;
    if (value) learned.add(lemma); else learned.delete(lemma);
    for (const record of records) if (record.item.lemma === lemma) paint(record);
    if (!value) refreshSkipped();
  }
  async function click(event) {
    const wrapper = event.target.closest?.('[data-echo-word]');
    if (!enabled || !wrapper || (wrapper.closest('a') && !event.altKey) || !getSelection().isCollapsed) return;
    event.preventDefault(); event.stopPropagation();
    const lemma = wrapper.dataset.echoWord, value = !learned.has(lemma), version = epoch;
    if (saving.has(lemma)) return;
    saving.add(lemma); learnedRevision++;
    // Wait for persistence before changing the visible state.
    try {
      const result = await chrome.runtime.sendMessage({type: 'set-learned', lemma, learned: value});
      if (result.error) throw new Error(result.error);
      if (enabled && version === epoch) learnedChange(lemma, value);
    } catch (error) { lastError = error.message; wrapper.title = `保存失败：${lastError}`; }
    finally { saving.delete(lemma); }
  }
  function stop() {
    enabled = false; epoch++; clearInterval(syncTimer); clearTimeout(scanTimer); clearTimeout(retryTimer); interrupted.clear();
    watcher?.disconnect(); visible?.disconnect(); port?.disconnect(); port = null;
    document.removeEventListener('click', click, true);
    document.removeEventListener('scroll', scroll, true);
    for (const record of records) for (const element of record.elements) {
      if (element.isConnected) element.replaceWith(document.createTextNode(element.querySelector('.echo-reader-en')?.textContent || ''));
    }
    records.clear(); style.remove(); queue = []; current = null; paused = false;
    seen = new WeakSet(); scheduled = new WeakSet();
  }
  async function start() {
    if (enabled) return;
    enabled = true; lastError = ''; const version = ++epoch;
    if (await syncLearned() === false) {
      const error = new Error(lastError || '无法读取 Echo 学习记录'); stop(); throw error;
    }
    if (!enabled || epoch !== version) return;
    (document.head || document.documentElement).append(style);
    connect();
    visible = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) { visible.unobserve(entry.target); collect(entry.target); }
    }, {rootMargin: '200px'});
    watcher = new MutationObserver(mutations => {
      const roots = [];
      for (const mutation of mutations) {
        if (mutation.target.parentElement?.closest('[data-echo-word],[data-echo-ui]')) continue;
        if (mutation.type === 'characterData') { seen.delete(mutation.target); roots.push(mutation.target); }
        else for (const node of mutation.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE && !node.matches('[data-echo-word],[data-echo-ui]')) roots.push(node);
          else if (eligible(node) && !seen.has(node)) roots.push(node);
        }
      }
      clearTimeout(scanTimer);
      // Observe roots immediately (no model call); visible callback bounds work.
      for (const root of roots) {
        const block = root.parentElement?.closest(BLOCK);
        if (block) scheduled.delete(block);
        discover(root);
      }
    });
    watcher.observe(document.body, {childList: true, subtree: true, characterData: true});
    document.addEventListener('click', click, true);
    document.addEventListener('scroll', scroll, {capture: true, passive: true});
    syncTimer = setInterval(() => { void syncLearned(); }, 15000);
    discover();
  }

  // WEB-007: completed DOM annotations survive worker suspension. Only interrupted
  // chunks are retried; new request IDs reject responses from the old stream.
  function liveJob(job) {
    return job.parts.some(p => p.node.isConnected) ||
      [...job.records.values()].some(r => r.elements.some(e => e.isConnected));
  }
  function remember(job) {
    paused = true; failPending(job, 'connection_error');
    interrupted.add(job);
    // Bound retained retry descriptors as well as the worker's queue.
    if (interrupted.size > 16) interrupted.delete(interrupted.values().next().value);
    scheduleRecovery();
  }
  function scheduleRecovery() {
    if (!enabled || retryTimer || document.hidden) return;
    retryTimer = setTimeout(() => { retryTimer = null; void recover(); }, 3000);
  }
  async function recover(wake = false) {
    const version = epoch;
    if (!enabled || document.hidden) return;
    if (!port) connect();
    if (!await syncLearned() || !enabled || epoch !== version) { scheduleRecovery(); return; }
    paused = false; lastError = '';
    for (const job of interrupted) {
      if (!liveJob(job)) { interrupted.delete(job); continue; }
      if (wake) job.retries = 0;
      if ((job.retries || 0) >= 3) continue;
      job.retries = (job.retries || 0) + 1;
      job.id = ++serial; queue.push(job); interrupted.delete(job);
    }
    pump(); discover();
  }
  function connectionLost(active) {
    if (!active || active !== port) return;
    lastError = '伴读连接正在恢复';
    if (current) remember(current);
    port = null; current = null;
    active.disconnect(); scheduleRecovery();
  }
  function connect() {
    const version = epoch;
    const active = chrome.runtime.connect({name: 'echo-reader'}); port = active;
    port.onMessage.addListener(message => {
      if (!enabled || version !== epoch || port !== active) return;
      if (message.learned) {
        const {lemma, value} = message.learned;
        learnedChange(lemma, value);
        return;
      }
      if (!current || message.id !== current.id) return;
      if (message.items) {
        if (message.phase === 'dictionary' && !current.records.size) render(current, message.items);
        else for (const item of message.items) {
          const record = current.records.get(item.start); if (record) paint(record, item);
        }
      }
      if (message.done) current.complete = true;
      if (message.error) { lastError = message.error; current.failed = true; }
      if (message.finished) {
        if (!current.complete || current.failed) remember(current);
        else lastError = '';
        current = null; refreshSkipped(); pump();
      }
    });
    active.onDisconnect.addListener(() => {
      if (version === epoch && enabled) connectionLost(active);
    });

  }
  const wake = () => { if (enabled && !document.hidden) void recover(true); };
  document.addEventListener('visibilitychange', wake);
  window.addEventListener('pageshow', wake);
  window.addEventListener('online', wake);

  globalThis.__echoReader = {start, stop};
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.connection && enabled) { stop(); void start().catch(error => { lastError = error.message; }); }
  });
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.type === 'echo-state') reply({enabled, error: lastError});
    if (message.type === 'echo-toggle') {
      const promise = message.enabled ? start() : Promise.resolve(stop());
      promise.then(() => reply({enabled})).catch(error => reply({error: error.message})); return true;
    }
  });
  chrome.storage.local.get(['sites', 'autoEnable']).then(({sites = [], autoEnable = false}) => {
    if (autoEnable || sites.includes(`${location.origin}/*`)) return start();
  }).catch(error => { lastError = error.message; });
})();
