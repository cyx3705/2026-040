// INPUT-001/002: actual DOM controller + production key handlers, no OS changes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = name => fs.readFileSync(path.join(__dirname, '../src/echo_app/static', name), 'utf8');
const app = source('app.js');
class Target {
  constructor() { this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, detail = {}) {
    const event = { key: '', isTrusted: true, preventDefault() { this.prevented = true; this.defaultPrevented = true; }, ...detail };
    for (const fn of this.listeners[type] || []) fn(event);
    return event;
  }
  dispatchEvent(event) { return this.dispatch(event.type, event); }
}
function harness(native = {}) {
  const input = Object.assign(new Target(), { value: '', selectionStart: 0, selectionEnd: 0,
    dataset: {}, setSelectionRange(a, b, direction = 'none') { this.selectionStart = a; this.selectionEnd = b; this.selectionDirection = direction; },
    setRangeText(text, start, end, selectionMode) {
      const left = this.selectionStart, right = this.selectionEnd, shift = text.length - (end - start);
      this.value = this.value.slice(0, start) + text + this.value.slice(end);
      if (selectionMode === 'preserve') {
        const position = old => old > end ? old + shift : old > start ? start + text.length : old;
        this.setSelectionRange(position(left), position(right));
      } else this.setSelectionRange(start + text.length, start + text.length);
    } });
  const document = Object.assign(new Target(), { activeElement: input, hidden: false, title: 'Echo', hasFocus: () => true });
  let now = 100, timer = 0, frames = [], submits = 0, predictions = 0;
  const requests = [], timers = new Map(), hint = {};
  const context = vm.createContext({
    input, document, window: new Target(), console,
    InputEvent: class { constructor(type, data) { this.type = type; Object.assign(this, data); } },
    crypto: { randomUUID: () => '12345678-1234-1234-1234-123456789abc' },
    performance: { now: () => now },
    requestAnimationFrame: fn => { frames.push(fn); },
    setTimeout: (fn, ms) => { timers.set(++timer, { fn, ms }); return timer; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      const body = JSON.parse(options.body); requests.push(body);
      if (body.action === 'acquire' && !native.verified) input.dispatch('keydown', { key: 'F22' });
      return { ok: true, json: async () => ({ owner: 1, active: true, ready: body.action !== 'acquire', chinese: false, ...native }) };
    },
    inputMode: null, inputTranslation: null, suggestions: [], suggestionsFor: null, sel: 0, PARTIAL: /[A-Za-z']+$/,
    autosize() {}, updateSend() {}, renderSuggest() {},
    hideSuggest() { context.suggestions = []; context.suggestionsFor = null; },
    fetchSuggest() { ++predictions; }, submit() { ++submits; }, suggestScheduledAt: 0,
  });
  // Register production handler first, matching the page's listener order.
  vm.runInContext(app.slice(app.indexOf('input.addEventListener("keydown"'), app.indexOf('function autosize()')), context);
  vm.runInContext(app.slice(app.indexOf('function candidateIsCurrent()'), app.indexOf('// ---- 逐词释义')), context);
  vm.runInContext(source('composer_input.js'), context);
  const Controller = vm.runInContext('EchoInputMode', context);
  const mode = new Controller(input, hint, () => {}); context.inputMode = mode;
  mode.nativeBounds = () => ({ left: 20, top: 400, width: 300, height: 26,
    viewport_width: 1280, viewport_height: 720, font_size: 17, color: 0, background: 0xffffff });
  return { input, mode, hint, context, document, requests, timers,
    frames: () => { const list = frames; frames = []; list.forEach(fn => fn()); },
    time: value => { now = value; },
    counts: () => ({ submits, predictions }),
    words(text, word = 'make') {
      input.value = text; input.setSelectionRange(text.length, text.length);
      context.suggestions = [{ word, kind: 'word' }]; context.suggestionsFor = text;
    },
  };
}
(async () => {
  const h = harness(), { input, mode } = h;
  assert.equal(input.readOnly, true);
  await mode.start(); assert.equal(mode.owner, 1); assert.equal(input.readOnly, false);
  assert.equal(h.requests[0].action, 'acquire'); assert.equal(h.requests[1].action, 'confirm');
  assert.equal(h.requests[1].bounds.width, 300, 'confirmation carries the inline editor anchor');
  h.words('I want to mak');
  assert.equal(input.dispatch('keydown', { key: ' ', shiftKey: true }).prevented, true);
  assert.equal(input.value, 'I want to make '); assert.equal(h.counts().predictions, 1);
  input.dispatch('keydown', { key: ' ', shiftKey: true, repeat: true }); assert.equal(h.counts().predictions, 1);
  h.words('I want to mak'); h.context.suggestionsFor = 'old';
  assert.equal(input.dispatch('keydown', { key: ' ' }).prevented, undefined);
  h.words('I want to mak'); input.setSelectionRange(1, 1);
  assert.equal(input.dispatch('keydown', { key: ' ' }).prevented, undefined);
  h.words('I want to mak');
  assert.equal(input.dispatch('keydown', { key: ' ' }).prevented, undefined);
  assert.equal(input.dispatch('keydown', { key: ' ', ctrlKey: true }).prevented, undefined);
  input.dispatch('keydown', { key: 'Tab' }); assert.equal(input.value, 'I want to make ');
  h.words('I want to mak'); h.context.suggestions.push({ word: 'maker', kind: 'word' }); h.context.sel = 1;
  input.dispatch('keydown', { key: ' ', shiftKey: true }); assert.equal(input.value, 'I want to maker '); h.context.sel = 0;
  h.words('I agree ', '.'); h.context.suggestions[0].kind = 'punct';
  input.dispatch('keydown', { key: ' ', shiftKey: true }); assert.equal(input.value, 'I agree. ');
  input.dispatch('keydown', { key: 'Enter' }); assert.equal(h.counts().submits, 1);

  h.time(200); input.dispatch('keydown', { key: 'F24' }); assert.equal(mode.chinese, true);
  mode.apply({ active: true, ready: true, chinese: false }, 150);
  assert.equal(mode.chinese, true, 'old heartbeat cannot override a newer native marker');
  assert.equal(input.dispatch('keydown', { key: ' ' }).prevented, undefined);
  input.dispatch('keydown', { key: 'Enter' }); assert.equal(h.counts().submits, 1);
  const before = input.value;
  input.dispatch('compositionstart'); input.value += '未确认';
  h.time(210); input.dispatch('keydown', { key: 'F23' });
  assert.equal(mode.chinese, false); assert.equal(input.value, before);
  input.dispatch('compositionend');
  input.value += '未确认'; input.dispatch('input', { inputType: 'insertFromComposition', data: '未确认' });
  assert.equal(input.value, before, 'late non-cancelable IME input must be removed');
  h.frames();
  input.dispatch('keydown', { key: 'F24' });
  input.dispatch('compositionstart'); input.value += '中文'; input.dispatch('compositionend');
  input.dispatch('keydown', { key: 'F23' }); assert.equal(input.value, before + '中文');
  assert.equal(input.dispatch('beforeinput', { inputType: 'insertText', data: '中' }).prevented, true);
  mode.stop(); assert.equal(input.readOnly, true); assert.equal(mode.owner, 0);
  assert.equal(h.requests.at(-1).action, 'release');
  h.words('I ');
  input.dispatch('keydown', { key: 'Process', code: 'KeyL' }); assert.equal(input.value, 'I l');
  input.dispatch('keydown', { key: 'o', code: 'KeyO' }); assert.equal(input.value, 'I lo');
  input.dispatch('keydown', { key: 'Backspace' }); assert.equal(input.value, 'I l');
  input.dispatch('keydown', { key: 'z', ctrlKey: true }); assert.equal(input.value, 'I lo');
  h.words('I want to mak'); input.dispatch('keydown', { key: ' ', shiftKey: true }); assert.equal(input.value, 'I want to make ');
  h.words('I '); h.context.suggestions = [];
  input.dispatch('keydown', { key: ' ' }); assert.equal(input.value, 'I  ');
  input.dispatch('paste', { clipboardData: { getData: () => 'English' } }); assert.equal(input.value, 'I  English');
  input.dispatch('paste', { clipboardData: { getData: () => '中文' } }); assert.equal(input.value, 'I  English');
  assert.equal(input.readOnly, true, 'fallback edits must never enable the system IME');
  // Leaving while acquisition is in flight must release the late native lease.
  const late = harness(); let complete;
  late.context.fetch = (url, options) => {
    const body = JSON.parse(options.body); late.requests.push(body);
    return new Promise(resolve => { complete = () => resolve({ ok: true, json: async () => ({ owner: 99 }) }); });
  };
  const pending = late.mode.start(); late.document.activeElement = null; late.mode.stop(); complete(); await pending;
  assert.equal(late.mode.owner, 0); assert.equal(late.input.readOnly, true);
  assert.equal(late.requests.at(-1).owner, 99);

  // Native proxy focus is intentionally outside the DOM textarea. It must survive
  // the DOM blur, then insert confirmed text once at the original selection.
  const native = { verified: true, proxy: true, committed: '' };
  const proxy = harness(native);
  await proxy.mode.start();
  assert.equal(proxy.mode.owner, 1);
  assert.equal(proxy.input.readOnly, true);
  assert.equal(proxy.mode.fallback, true);
  assert.equal(proxy.document.title, 'Echo');
  proxy.words('Hello world'); proxy.input.setSelectionRange(6, 11);
  proxy.input.dispatch('keydown', { key: 'F24' });
  proxy.context.window.dispatch('blur'); proxy.input.dispatch('blur');
  assert.equal(proxy.mode.owner, 1, 'owned native input must not be released on its own focus handoff');
  proxy.document.hasFocus = () => false;
  proxy.mode.pump(proxy.mode.epoch);
  assert.ok([...proxy.timers.values()].some(timer => timer.ms === 250), 'proxy heartbeat survives native focus');
  native.committed = '你好';
  proxy.input.dispatch('keydown', { key: 'F23' });
  assert.equal(proxy.mode.composing, true, 'sending waits for the pending native commit');
  for (let i = 0; i < 10; ++i) await Promise.resolve();
  assert.equal(proxy.input.value, 'Hello 你好');
  assert.equal(proxy.input.selectionStart, proxy.input.value.length, 'typing resumes after the committed Chinese');
  assert.equal(proxy.input.selectionEnd, proxy.input.value.length);
  assert.equal(proxy.mode.chinese, false); assert.equal(proxy.mode.composing, false);
  proxy.mode.apply({ ...native, active: true, ready: true, chinese: false });
  assert.equal(proxy.input.value, 'Hello 你好', 'duplicate native snapshots cannot insert text twice');
  proxy.mode.apply({ ...native, committed: '', active: true, ready: true, chinese: false });
  assert.equal(proxy.input.value, 'Hello 你好', 'older cumulative text cannot roll back an acknowledged commit');
  proxy.input.setSelectionRange(proxy.input.value.length, proxy.input.value.length);
  proxy.input.dispatch('keydown', { key: 'F24' });
  proxy.mode.apply({ ...native, active: false, ready: false, chinese: false, committed: '你好世界' });
  assert.equal(proxy.input.value, 'Hello 你好世界', 'focus revocation delivers final confirmed text');
  assert.equal(proxy.mode.owner, 0);
  const cancel = harness({ verified: true, proxy: true, committed: '' });
  await cancel.mode.start(); cancel.words('Hello ');
  cancel.input.dispatch('keydown', { key: 'F24' });
  cancel.mode.apply({ proxy: true, active: true, ready: true, chinese: false, committed: '' });
  assert.equal(cancel.mode.proxyAnchor, null, 'cancelled preedit leaves no stale insertion anchor');
  assert.equal(cancel.input.value, 'Hello ');
  cancel.mode.apply({ proxy: true, active: true, ready: false, failed: true });
  assert.match(cancel.hint.textContent, /未能启用/);
  // Readonly Chromium does not move an insertion point on arrow keys. The view
  // must move it explicitly, preserving selection anchors and UTF-16 boundaries.
  const View = vm.runInContext('EchoComposerView', h.context);
  const view = Object.create(View.prototype);
  view.input = input; view.mode = () => mode; view.schedule = () => {};
  input.readOnly = true; mode.chinese = mode.composing = false;
  input.value = 'a😀b'; input.setSelectionRange(4, 4);
  const navigate = (key, extra = {}) => view.navigate({ key, preventDefault() {}, ...extra });
  navigate('ArrowLeft'); assert.equal(input.selectionStart, 3);
  navigate('ArrowLeft', { shiftKey: true });
  assert.equal(input.selectionStart, 1); assert.equal(input.selectionEnd, 3);
  navigate('ArrowRight', { shiftKey: true });
  assert.equal(input.selectionStart, 3); assert.equal(input.selectionEnd, 3);
  input.setSelectionRange(1, 3); navigate('ArrowRight');
  assert.equal(input.selectionStart, 3); assert.equal(input.selectionEnd, 3);
  mode.chinese = true; navigate('ArrowLeft'); assert.equal(input.selectionStart, 3);
  mode.chinese = false; navigate('ArrowLeft', { ctrlKey: true }); assert.equal(input.selectionStart, 3);
  input.readOnly = false; navigate('ArrowLeft'); assert.equal(input.selectionStart, 3);
  // Ctrl release only returns Chinese. Several sessions can form one word;
  // only a later plain Space identifies the user's intended word boundary.
  const session = harness({ verified: true, proxy: true });
  const WordTranslator = vm.runInContext('EchoInputTranslation', session.context);
  const wordRequests = [];
  const wordTranslator = new WordTranslator(session.input,
    request => { wordRequests.push(request); return new Promise(() => {}); }, () => 'chat', () => {});
  session.context.inputTranslation = wordTranslator;
  await session.mode.start(); session.words('I ');
  session.input.dispatch('keydown', { key: 'F24' });
  session.mode.apply({ active: true, ready: true, proxy: true, chinese: true, committed: '苹' });
  session.mode.apply({ active: true, ready: true, proxy: true, chinese: false, committed: '苹' });
  assert.equal(wordRequests.length, 0, 'Ctrl release is not a word boundary');
  session.input.dispatch('keydown', { key: 'F24' });
  session.mode.apply({ active: true, ready: true, proxy: true, chinese: true, committed: '苹果' });
  session.mode.apply({ active: true, ready: true, proxy: true, chinese: false, committed: '苹果' });
  session.mode.apply({ active: true, ready: true, proxy: true, chinese: false, committed: '苹果' });
  assert.equal(wordRequests.length, 0, 'native heartbeats must not translate');
  assert.equal(wordTranslator.awaitingBoundary, true);
  session.input.dispatch('keydown', { key: ' ', shiftKey: true });
  assert.equal(wordRequests.length, 0, 'Shift+Space is not the translation shortcut');
  session.input.dispatch('keydown', { key: 'Backspace' }); // remove literal Shift+Space
  session.input.dispatch('keydown', { key: ' ' });
  assert.equal(wordRequests.length, 1);
  assert.equal(wordRequests[0].chinese, '苹果', 'translate the whole word, not two separate characters');
  assert.equal(wordRequests[0].before, 'I '); assert.equal(wordRequests[0].after, ' ');
  assert.equal(session.input.value, 'I 苹果 ');
  session.input.dispatch('keydown', { key: ' ', repeat: true });
  assert.equal(wordRequests.length, 1, 'repeated space cannot request the same translation twice');

  const selected = harness();
  const selectedTranslator = new WordTranslator(selected.input, () => { throw new Error('must not translate'); }, () => 'chat', () => {});
  selected.context.inputTranslation = selectedTranslator;
  selected.words('苹果'); selected.input.setSelectionRange(0, 2);
  selected.input.dispatch('keydown', { key: ' ' });
  assert.equal(selected.input.value, ' ', 'Space replacing a selection is ordinary editing');

  const quick = harness(); let nativeReady = false;
  const quickRequests = [];
  const quickTranslator = new WordTranslator(quick.input,
    request => { quickRequests.push(request); return new Promise(() => {}); },
    () => 'chat', () => {}, () => nativeReady);
  quick.context.inputTranslation = quickTranslator;
  quick.words('I '); quick.mode.proxyPending = quick.mode.composing = true;
  quick.input.dispatch('keydown', { key: ' ' });
  assert.equal(quick.input.value, 'I ', 'a quick Ctrl-up Space waits for native text');
  assert.equal(quickRequests.length, 0);
  quick.mode.syncCommitted('苹果');
  quickTranslator.flushWordBoundary(); assert.equal(quickRequests.length, 0);
  quick.mode.proxyPending = quick.mode.composing = false; nativeReady = true;
  quickTranslator.flushWordBoundary(); quickTranslator.flushWordBoundary();
  assert.equal(quick.input.value, 'I 苹果 ');
  assert.equal(quickRequests.length, 1); assert.equal(quickRequests[0].chinese, '苹果');

  const Translator = vm.runInContext('EchoInputTranslation', h.context);
  const jobs = []; let contextId = 'chat1', changed = [];
  const translator = new Translator(input, request => new Promise(resolve => jobs.push({ request, resolve })),
    () => contextId, reason => changed.push(reason));
  h.context.inputTranslation = translator;
  const settle = async () => { for (let i = 0; i < 12; ++i) await Promise.resolve(); };
  const queue = text => {
    input.value = text; input.setSelectionRange(text.length, text.length);
    input.dispatch('input');
    translator.enqueue({ start: text.indexOf('苹果'), end: text.indexOf('苹果') + 2 });
  };
  queue('I want 苹果'); assert.equal(translator.pending, true);
  input.setRangeText(' today', input.value.length, input.value.length, 'end'); input.dispatch('input');
  jobs[0].resolve({ word: 'apple' }); await settle();
  assert.equal(input.value, 'I want apple today', 'typing after the Chinese must survive replacement');
  assert.equal(input.selectionStart, input.value.length);
  assert.equal(translator.pending, false);
  queue('苹果');
  jobs[1].resolve({ word: 'apple' }); await settle();
  assert.equal(input.value, 'Apple ', 'capitalize sentence start and leave a word boundary');
  queue('I want 苹果');
  input.setRangeText('pear', 7, 9, 'end'); input.dispatch('input');
  jobs[2].resolve({ word: 'apple' }); await settle();
  assert.equal(input.value, 'I want pear', 'late translation must not overwrite a manual edit');
  queue('I want 苹果'); contextId = 'chat2';
  jobs[3].resolve({ word: 'apple' }); await settle();
  assert.equal(input.value, 'I want 苹果', 'a different conversation invalidates the result');
  queue('I want 苹果');
  jobs[4].resolve({ word: null, reason: 'unresolved' }); await settle();
  assert.equal(input.value, 'I want 苹果'); assert.ok(changed.includes('unresolved'));
  queue('I want 苹果');
  input.setRangeText('Today ', 0, 0, 'end'); input.dispatch('input');
  translator.enqueue({ start: 13, end: 15 });
  assert.equal(jobs.length, 6, 'translation requests are serialized');
  jobs[5].resolve({ word: 'apple' }); await settle();
  assert.equal(input.value, 'Today I want apple ');
  assert.equal(translator.pending, false, 'overlapping queued work is invalidated by replacement');
  let canApply = true;
  translator.canApply = () => canApply;
  queue('I want 苹果'); canApply = false;
  jobs[6].resolve({ word: 'apple' }); await settle();
  assert.equal(input.value, 'I want 苹果', 'a second native Ctrl session keeps its insertion anchor stable');
  assert.equal(translator.pending, true);
  canApply = true; await translator.pump();
  assert.equal(input.value, 'I want apple ');
  assert.equal(jobs.length, 7, 'resuming a deferred result must not run the model twice');
  console.log('PASS Shift+Space/Tab and literal Space, IME boundaries, heartbeat/focus races, native proxy commits, deduplication and cancellation');
  console.log('PASS readonly caret navigation, selection anchors, Unicode boundaries and Chinese-mode isolation');
  console.log('PASS Chinese word boundary on plain Space, zero translation on Ctrl release/IME/Shift+Space, queued choices and edit races');
})().catch(error => { console.error(error); process.exitCode = 1; });
