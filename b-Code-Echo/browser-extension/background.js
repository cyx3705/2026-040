/* WEB-001: fixed loopback API only; never proxy arbitrary page URLs. */
const API = 'http://127.0.0.1:8040';
const ports = new Set();
const jobs = [];
let busy = false;

async function api(path, body, signal) {
  const response = await fetch(API + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body), signal,
  });
  if (!response.ok) throw new Error(`Echo 接口 ${response.status}`);
  return response;
}
function post(port, value) {
  try { port.postMessage(value); } catch { /* Tab navigated or disabled. */ }
}
async function drain() {
  if (busy) return;
  busy = true;
  try {
    while (jobs.length) {
      const {port, id, text} = jobs.shift();
      if (!ports.has(port)) continue;
      const controller = new AbortController();
      port.abort = controller;
      const timeout = setTimeout(() => controller.abort(), 90000);
      try {
        const response = await api('/api/reader/gloss', {text}, controller.signal);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const {value, done} = await reader.read();
          buffer += decoder.decode(value, {stream: !done});
          let split;
          while ((split = buffer.indexOf('\n\n')) >= 0) {
            const event = buffer.slice(0, split); buffer = buffer.slice(split + 2);
            if (event.startsWith('data: ')) post(port, {id, ...JSON.parse(event.slice(6))});
          }
          if (done) break;
        }
        post(port, {id, finished: true});
      } catch (error) {
        post(port, {id, error: `无法连接本机 Echo，或选义中断：${error.message}`, finished: true});
      } finally {
        clearTimeout(timeout); port.abort = null;
      }
    }
  } finally { busy = false; }
}

chrome.runtime.onConnect.addListener(port => {
  if (port.name !== 'echo-reader' || !port.sender?.tab) return;
  ports.add(port);
  port.onMessage.addListener(message => {
    if (message.type !== 'gloss' || typeof message.text !== 'string' ||
        message.text.length > 1600 || jobs.length >= 16) {
      post(port, {id: message.id, error: '伴读队列已满，请稍后重试', finished: true}); return;
    }
    jobs.push({port, id: message.id, text: message.text});
    void drain();
  });
  port.onDisconnect.addListener(() => {
    ports.delete(port); port.abort?.abort();
    for (let i = jobs.length - 1; i >= 0; i--) if (jobs[i].port === port) jobs.splice(i, 1);
  });
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const task = async () => {
    if (message.type === 'status') return (await api('/api/status')).json();
    if (message.type === 'learned') return (await api('/api/reader/learned')).json();
    if (message.type === 'set-learned' && typeof message.lemma === 'string' &&
        /^[a-z][a-z'’-]{0,79}$/i.test(message.lemma) && typeof message.learned === 'boolean') {
      const value = await (await api('/api/learned', {lemma: message.lemma, learned: message.learned})).json();
      for (const port of ports) post(port, {learned: {lemma: message.lemma, value: message.learned}});
      return value;
    }
    throw new Error('未知伴读操作');
  };
  task().then(data => reply({data})).catch(error => reply({error: error.message}));
  return true;
});

// Optional persistent access is granted per site from the popup. Register only
// sites the learner explicitly enabled; activeTab alone handles one-page mode.
async function registerSites() {
  const {sites = []} = await chrome.storage.local.get('sites');
  const registered = await chrome.scripting.getRegisteredContentScripts();
  if (registered.length) await chrome.scripting.unregisterContentScripts({ids: registered.map(s => s.id)});
  const allowed = [];
  for (const origin of sites) {
    if (await chrome.permissions.contains({origins: [origin]})) allowed.push(origin);
  }
  if (allowed.length) await chrome.scripting.registerContentScripts([{
    id: 'echo-sites', matches: allowed, js: ['reader-core.js', 'content.js'],
    runAt: 'document_idle', persistAcrossSessions: true,
  }]);
}
let registration = Promise.resolve();
function queueRegistration() {
  registration = registration.catch(() => {}).then(registerSites);
}
chrome.runtime.onInstalled.addListener(queueRegistration);
chrome.runtime.onStartup.addListener(queueRegistration);
chrome.permissions.onRemoved.addListener(queueRegistration);
chrome.storage.onChanged.addListener(changes => { if (changes.sites) queueRegistration(); });
