/* YT-003: one bounded queue. Echo/C++ owns inference and the shared DB. */
importScripts('connection.js');
const api = EchoConnection.api;
const ports = new Set(), jobs = [];
let busy = false, connectionEpoch = 0;
function post(port, data) { try { port.postMessage(data); } catch { /* Tab closed. */ } }
async function drain() {
  if (busy) return;
  busy = true;
  try {
    while (jobs.length) {
      const job = jobs.shift(), {port, id, text, session} = job;
      if (!ports.has(port)) continue;
      port.active = job; job.controller = new AbortController();
      const timeout = setTimeout(() => job.controller.abort(), 90000);
      try {
        const response = await api('/api/reader/gloss', session ? {text, session} : {text}, job.controller.signal);
        const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '', complete = false;
        while (true) {
          const {value, done} = await reader.read(); buffer += decoder.decode(value, {stream: !done});
          let split;
          while ((split = buffer.indexOf('\n\n')) >= 0) {
            const event = buffer.slice(0, split); buffer = buffer.slice(split + 2);
            if (event.startsWith('data: ') && !job.cancelled) {
              const value = JSON.parse(event.slice(6));
              if (value.done) complete = true;
              post(port, {id, ...value});
            }
          }
          if (done) break;
        }
        if (!job.cancelled && !complete) throw new Error('stream_interrupted');
        if (!job.cancelled) post(port, {id, finished: true});
      } catch (error) {
        if (!job.cancelled) post(port, {id, error: `Echo 选义中断：${error.message}`, finished: true});
      } finally { clearTimeout(timeout); port.active = null; }
    }
  } finally { busy = false; }
}
function cancel(port, id) {
  if (port.active && (id === undefined || port.active.id === id)) {
    port.active.cancelled = true; port.active.controller.abort();
  }
  for (let i = jobs.length - 1; i >= 0; i--) if (jobs[i].port === port && (id === undefined || jobs[i].id === id)) jobs.splice(i, 1);
}
chrome.runtime.onConnect.addListener(port => {
  if (!['echo-reader', 'echo-youtube'].includes(port.name) || !port.sender?.tab) return;
  ports.add(port);
  port.onMessage.addListener(message => {
    if (message.type === 'cancel') { cancel(port, message.id); return; }
    if (message.type !== 'gloss' || !['string', 'number'].includes(typeof message.id) || typeof message.text !== 'string' ||
        !message.text.length || message.text.length > 1600 || jobs.length >= 16) {
      post(port, {id: message.id, error: '伴读请求超限，请稍后重试', finished: true}); return;
    }
    // A page/video identity lets the host resume that context's saved KV.
    const session = typeof message.session === 'string' && message.session.length <= 300 ? message.session : undefined;
    const job = {port, id: message.id, text: message.text, session};
    if (message.priority) jobs.unshift(job); else jobs.push(job);
    void drain();
  });
  port.onDisconnect.addListener(() => { ports.delete(port); cancel(port); });
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const version = connectionEpoch;
  if (!['echo-youtube-status', 'echo-youtube-learned', 'echo-youtube-set-learned', 'echo-connection-save', 'echo-connection-status', 'status', 'learned', 'set-learned'].includes(message.type)) return;
  const task = async () => {
    if (message.type === 'echo-connection-save') {
      if (sender.url !== chrome.runtime.getURL('popup.html')) throw new Error('只能在插件设置中修改连接');
      const value = EchoConnection.normalize(message.connection);
      if (!await chrome.permissions.contains({origins: [`${value.url}/*`]})) throw new Error('未授予主机访问权限');
      await chrome.storage.local.set({connection: value}); return {ok: true};
    }
    if (message.type === 'echo-connection-status') return (await api('/api/status')).json();
    if (['echo-youtube-status', 'status'].includes(message.type)) return (await api('/api/status')).json();
    if (['echo-youtube-learned', 'learned'].includes(message.type)) return (await api('/api/reader/learned')).json();
    if (!/^[a-z][a-z'’-]{0,79}$/i.test(message.lemma || '') || typeof message.learned !== 'boolean') throw new Error('无效学习记录');
    const value = await (await api('/api/learned', {lemma: message.lemma, learned: message.learned})).json();
    if (version === connectionEpoch) for (const port of ports) post(port, {learned: {lemma: message.lemma, value: message.learned}});
    return value;
  };
  task().then(data => reply({data})).catch(error => reply({error: error.message}));
  return true;
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.connection) {
    connectionEpoch++;
    for (const port of ports) cancel(port);
  }
});

// WEB-005: all-web access is optional and requested by a popup user gesture.
// Turning automatic reading off retains grants and per-site preferences.
async function registerSites() {
  const {sites = [], autoEnable = false} = await chrome.storage.local.get(['sites', 'autoEnable']);
  const registered = (await chrome.scripting.getRegisteredContentScripts()).filter(s => s.id === 'echo-sites');
  if (registered.length) await chrome.scripting.unregisterContentScripts({ids: registered.map(s => s.id)});
  const allowed = [];
  if (autoEnable && await chrome.permissions.contains({origins: ['http://*/*', 'https://*/*']})) {
    allowed.push('http://*/*', 'https://*/*');
  } else {
    // A browser-side permission revocation must not leave a misleading switch.
    if (autoEnable) await chrome.storage.local.set({autoEnable: false});
    for (const origin of sites) {
      if (await chrome.permissions.contains({origins: [origin]})) allowed.push(origin);
    }
  }
  if (allowed.length) await chrome.scripting.registerContentScripts([{
    id: 'echo-sites', matches: allowed, js: ['reader-core.js', 'content.js'],
    excludeMatches: ['http://127.0.0.1/*', 'https://127.0.0.1/*', 'http://localhost/*', 'https://localhost/*', `${(await EchoConnection.settings()).url}/*`],
    runAt: 'document_idle', persistAcrossSessions: true,
  }]);
}
let registration = Promise.resolve();
function queueRegistration() {
  registration = registration.catch(() => {}).then(registerSites);
}
chrome.runtime.onInstalled.addListener(queueRegistration);
chrome.runtime.onStartup.addListener(queueRegistration);
chrome.permissions.onAdded.addListener(queueRegistration);
chrome.permissions.onRemoved.addListener(queueRegistration);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && (changes.sites || changes.autoEnable || changes.connection)) queueRegistration();
});
