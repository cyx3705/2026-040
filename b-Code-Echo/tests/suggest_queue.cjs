// PERF-004/PRED-002: execute the actual frontend scheduler with virtual time.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/echo_app/static/app.js'), 'utf8');
let now = 100, timerId = 0, outstanding = 0, maximum = 0, displayed = [];
const timers = new Map(), requests = [], elements = new Map();
const context = vm.createContext({
  busy: false, switching: false, ready: true, input: { value: 'I want to m' },
  inputMode: null, inputTranslation: null,
  history: [], level: 'cefr:A2', replyLength: 0, plan: null, debugSettings: { candidates: 5 },
  performance: { now: () => now },
  AbortController,
  setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, at: now + delay }); return id; },
  clearTimeout: (id) => timers.delete(id),
  $: (id) => {
    if (!elements.has(id)) elements.set(id, { classList: { add() {}, remove() {} } });
    return elements.get(id);
  },
  fetch: (url, options) => new Promise((resolve, reject) => {
    ++outstanding; maximum = Math.max(maximum, outstanding);
    options.signal.addEventListener('abort', () => { --outstanding; reject(Object.assign(new Error('timeout'), {name:'AbortError'})); });
    requests.push({ body: JSON.parse(options.body),
      finish: (items, ok = true) => { --outstanding; resolve({ ok, json: async () => items }); },
      fail: () => { --outstanding; reject(new Error('offline')); } });
  }),
  renderSuggest: () => displayed.push(vm.runInContext('suggestions.map(s => s.word).join(",")', context)),
  hideSuggest: () => displayed.push('hidden'),
});
vm.runInContext(source.slice(source.indexOf('let suggestions ='), source.indexOf('function hideSuggest()')), context);
const run = code => vm.runInContext(code, context);
async function settle() { for (let i=0; i<8; ++i) await Promise.resolve(); }
async function advance(ms) {
  now += ms;
  for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); }
  await settle();
}
(async () => {
  run('scheduleSuggest()'); await advance(24); assert.equal(requests.length, 0);
  await advance(1); assert.equal(requests.length, 1);
  context.input.value = 'I want to ma'; run('scheduleSuggest()');
  context.input.value = 'I want to mak'; run('scheduleSuggest()');
  assert.equal(timers.size, 2, 'only watchdog timers remain during an in-flight request');
  requests[0].finish([{ word: 'stale' }]); await settle();
  assert.equal(requests.length, 2); assert.equal(requests[1].body.text, 'I want to mak');
  assert.deepEqual(displayed.filter(Boolean), [], 'obsolete response must never render');
  await advance(7); requests[1].finish([{ word: 'make' }]); await settle();
  assert.deepEqual(displayed.filter(Boolean), ['make']); assert.equal(maximum, 1);
  assert.match(elements.get('debug-status').textContent, /7ms/);
  context.input.value = 'I '; run('scheduleSuggest()'); await advance(10);
  context.input.value = 'I like '; run('scheduleSuggest()'); await advance(24);
  assert.equal(requests.length, 2); await advance(1);
  assert.equal(requests[2].body.text, 'I like ');
  context.input.value = 'I love '; run('scheduleSuggest()');
  requests[2].fail(); await settle();
  assert.equal(requests[3].body.text, 'I love ');
  context.busy = true; requests[3].finish([{ word: 'old' }]); await settle();
  run('scheduleSuggest()'); await advance(25); assert.equal(requests.length, 4);
  context.busy = false; run('fetchSuggest()'); requests[4].finish([]); await settle();
  assert.equal(maximum, 1);
  assert.equal(run('suggestInFlight || suggestPending'), false);
  context.inputTranslation = { awaitingBoundary: true };
  run('scheduleSuggest()'); await advance(25);
  assert.equal(requests.length, 5, 'unconfirmed Chinese does not run English prediction');
  context.inputTranslation = null;
  run('fetchSuggest()'); await advance(1500);
  assert.equal(run('suggestState'), 'slow');
  assert.match(elements.get('debug-status').textContent, /等待/);
  requests[5].finish({ detail: '共用服务忙，请重试' }, false); await settle();
  assert.equal(run('suggestState'), 'error');
  assert.equal(run('suggestError'), '共用服务忙，请重试');
  assert.equal(run('suggestInFlight'), false);
  run('fetchSuggest()'); await advance(50000);
  assert.equal(run('suggestState'), 'error');
  assert.match(run('suggestError'), /超时/);
  assert.equal(run('suggestInFlight'), false);
  run('fetchSuggest()'); requests[7].finish([]); await settle();
  assert.equal(run('suggestState'), 'empty');
  assert.equal(maximum, 1);
  assert.equal(timers.size, 0);
  // Exercise actual status rendering and its explicit retry action.
  context.document = { createElement: () => ({ setAttribute() {}, append(...items) { this.children = items; },
    addEventListener(type, handler) { this[type] = handler; } }) };
  elements.get('suggest-list') || context.$('suggest-list');
  const list = elements.get('suggest-list');
  list.replaceChildren = note => { list.note = note; };
  context.input.focus = () => {};
  vm.runInContext(source.slice(source.indexOf('function renderSuggest()'), source.indexOf('function candidateIsCurrent()')), context);
  run('renderSuggest()'); assert.match(list.note.textContent, /暂无候选/);
  run('suggestState="error"; suggestError="服务忙"; renderSuggest()');
  assert.equal(list.note.textContent, '服务忙');
  list.note.children[1].click(); await advance(25);
  context.renderSuggest = () => {};
  requests[8].finish([{ word:'recovered' }]); await settle();
  assert.equal(run('suggestState'), 'ready');
  console.log('PASS queue, stale suppression, slow state, HTTP error, deadline abort, empty result and retry recovery');
})().catch(error => { console.error(error); process.exitCode = 1; });
