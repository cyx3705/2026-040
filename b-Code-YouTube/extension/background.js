/* YT-003: one bounded queue. Echo/C++ owns inference and the shared DB. */
const API = 'http://127.0.0.1:8040';
const ports = new Set(), jobs = [];
let busy = false;
async function api(path, body, signal = AbortSignal.timeout(10000)) {
  const response = await fetch(API + path, {method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body), signal});
  if (!response.ok) throw new Error(`Echo 接口 ${response.status}`);
  return response;
}
function post(port, data) { try { port.postMessage(data); } catch { /* Tab closed. */ } }
async function drain() {
  if (busy) return;
  busy = true;
  try {
    while (jobs.length) {
      const job = jobs.shift(), {port, id, text} = job;
      if (!ports.has(port)) continue;
      port.active = job; job.controller = new AbortController();
      const timeout = setTimeout(() => job.controller.abort(), 90000);
      try {
        const response = await api('/api/reader/gloss', {text}, job.controller.signal);
        const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
        while (true) {
          const {value, done} = await reader.read(); buffer += decoder.decode(value, {stream: !done});
          let split;
          while ((split = buffer.indexOf('\n\n')) >= 0) {
            const event = buffer.slice(0, split); buffer = buffer.slice(split + 2);
            if (event.startsWith('data: ') && !job.cancelled) post(port, {id, ...JSON.parse(event.slice(6))});
          }
          if (done) break;
        }
        if (!job.cancelled) post(port, {id, finished: true});
      } catch (error) {
        if (!job.cancelled) post(port, {id, error: `本机选义中断：${error.message}`, finished: true});
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
  if (port.name !== 'echo-youtube' || !port.sender?.tab) return;
  ports.add(port);
  port.onMessage.addListener(message => {
    if (message.type === 'cancel') { cancel(port, message.id); return; }
    if (message.type !== 'gloss' || typeof message.id !== 'string' || typeof message.text !== 'string' ||
        !message.text.length || message.text.length > 1600 || jobs.length >= 16) {
      post(port, {id: message.id, error: '字幕请求超限', finished: true}); return;
    }
    const job = {port, id: message.id, text: message.text};
    if (message.priority) jobs.unshift(job); else jobs.push(job);
    void drain();
  });
  port.onDisconnect.addListener(() => { ports.delete(port); cancel(port); });
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (!['echo-youtube-status', 'echo-youtube-learned', 'echo-youtube-set-learned'].includes(message.type)) return;
  const task = async () => {
    if (message.type === 'echo-youtube-status') return (await api('/api/status')).json();
    if (message.type === 'echo-youtube-learned') return (await api('/api/reader/learned')).json();
    if (!/^[a-z][a-z'’-]{0,79}$/i.test(message.lemma || '') || typeof message.learned !== 'boolean') throw new Error('无效学习记录');
    const value = await (await api('/api/learned', {lemma: message.lemma, learned: message.learned})).json();
    for (const port of ports) post(port, {learned: {lemma: message.lemma, value: message.learned}});
    return value;
  };
  task().then(data => reply({data})).catch(error => reply({error: error.message}));
  return true;
});
