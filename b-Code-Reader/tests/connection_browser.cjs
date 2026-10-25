/* NET-001 / ARCH-002: one real MV3 extension, real HTTP fixture, no user service/DB. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'echo-connection-test-'));
  const key = 'fixture-key-' + 'x'.repeat(32), known = new Set(), received = [];
  const server = http.createServer(async (req, res) => {
    const route = new URL(req.url, 'http://fixture').pathname;
    received.push({route, authorized: req.headers.authorization === `Bearer ${key}`});
    res.setHeader('Content-Type', 'application/json');
    if (req.headers.authorization !== `Bearer ${key}`) { res.writeHead(401); return res.end('{}'); }
    if (route === '/api/status') return res.end(JSON.stringify({model: 'shared-host'}));
    if (route === '/api/reader/learned') return res.end(JSON.stringify([...known].map(lemma => ({lemma}))));
    let text = ''; for await (const chunk of req) text += chunk;
    const body = JSON.parse(text || '{}');
    if (route === '/api/learned') {
      if (body.learned) known.add(body.lemma); else known.delete(body.lemma);
      return res.end('{"ok":true}');
    }
    if (route !== '/api/reader/gloss') { res.writeHead(404); return res.end('{}'); }
    const items = [...body.text.matchAll(/[A-Za-z]+/g)].map(match => ({start: match.index, end: match.index + match[0].length,
      word: match[0], lemma: match[0].toLowerCase(), gloss: known.has(match[0].toLowerCase()) ? '' : '银行',
      state: known.has(match[0].toLowerCase()) ? 'skipped_learned' : 'normal'}));
    res.setHeader('Content-Type', 'text/event-stream');
    res.end(`data: ${JSON.stringify({phase: 'dictionary', items})}\n\ndata: {"done":true}\n\n`);
  });
  let context;
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.2', resolve); });
    const url = `http://127.0.0.2:${server.address().port}`;
    const root = path.join(profile, 'extension');
    await fs.cp(path.resolve(__dirname, '../extension'), root, {recursive: true});
    const manifest = JSON.parse(await fs.readFile(path.join(root, 'manifest.json'), 'utf8'));
    manifest.host_permissions.push('http://127.0.0.2/*', 'https://learning.example/*');
    await fs.writeFile(path.join(root, 'manifest.json'), JSON.stringify(manifest));
    context = await chromium.launchPersistentContext(path.join(profile, 'profile'), {
      channel: 'msedge', headless: true,
      args: [`--disable-extensions-except=${root}`, `--load-extension=${root}`],
    });
    const activeWorker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    assert.equal(context.serviceWorkers().length, 1);
    const workers = {}, popups = {};
    for (const worker of [activeWorker]) {
      const type = 'Reader';
      workers[type] = worker;
      await worker.evaluate(({url, key}) => chrome.storage.local.set({enabled: false, connection: {url, key}}), {url, key: 'wrong-'.repeat(8)});
      const popup = await context.newPage(); popups[type] = popup;
      await popup.goto(new URL('popup.html', worker.url()).href);
      await popup.locator('summary').click();
      await popup.locator('#connection-url').fill(url);
      await popup.locator('#connection-key').fill(key);
      await popup.locator('#connection-save').click();
      await popup.waitForFunction(() => document.getElementById('connection-message').textContent.includes('已保存并连接'));
      assert(await popup.locator('#connection-open').isHidden(), 'remote host does not offer the local desktop UI');
      assert(!await popup.locator('body').textContent().then(text => text.includes(key)), 'key stays in password input, never visible status text');
      await popup.evaluate(() => { globalThis.originalRequest = chrome.permissions.request; chrome.permissions.request = async () => false; });
      await popup.locator('#connection-url').fill('http://192.0.2.10:8040');
      await popup.locator('#connection-save').click();
      await popup.waitForFunction(() => document.getElementById('connection-message').textContent.includes('未授予'));
      assert.equal(await worker.evaluate(async () => (await chrome.storage.local.get('connection')).connection.url), url, 'denied grant cannot replace connection');
      await popup.evaluate(() => { chrome.permissions.request = originalRequest; });
      await popup.locator('#connection-url').fill(url);
      await popup.locator('#connection-key').fill('wrong-'.repeat(8));
      await popup.locator('#connection-save').click();
      await popup.waitForFunction(() => document.getElementById('connection-message').textContent.includes('访问密钥不正确'));
      await popup.locator('#connection-key').fill(key);
      await popup.locator('#connection-save').click();
      await popup.waitForFunction(() => document.getElementById('connection-message').textContent.includes('已保存并连接'));
    }
    workers.YouTube = workers.Reader; popups.YouTube = popups.Reader;
    await context.route('https://learning.example/**', route => route.fulfill({contentType: 'text/html',
      body: '<!doctype html><html><body><p>The bank.</p></body></html>'}));
    const page = await context.newPage(); await page.goto('https://learning.example/');
    const tabId = await workers.Reader.evaluate(async () => (await chrome.tabs.query({})).find(tab => tab.url?.startsWith('https://learning.example/')).id);
    await workers.Reader.evaluate(async tabId => {
      await chrome.scripting.executeScript({target: {tabId}, files: ['reader-core.js', 'content.js']});
      await chrome.tabs.sendMessage(tabId, {type: 'echo-toggle', enabled: true});
    }, tabId);
    await page.locator('[data-echo-word="bank"] .echo-reader-zh').waitFor();
    assert.equal(await page.locator('[data-echo-word="bank"] .echo-reader-zh').textContent(), '银行');
    await page.locator('[data-echo-word="bank"]').click();
    await page.locator('[data-echo-word="bank"][data-known]').waitFor();
    assert(known.has('bank'));
    const fromYouTube = await popups.YouTube.evaluate(() => chrome.runtime.sendMessage({type: 'echo-youtube-learned'}));
    assert(fromYouTube.data.some(item => item.lemma === 'bank'), 'other extension reads the same host membership');
    await popups.YouTube.evaluate(() => chrome.runtime.sendMessage({type: 'echo-youtube-set-learned', lemma: 'bank', learned: false}));
    assert(!known.has('bank'));
    const fromReader = await popups.Reader.evaluate(() => chrome.runtime.sendMessage({type: 'learned'}));
    assert(!fromReader.data.some(item => item.lemma === 'bank'), 'reverse change reaches the same host');
    const denied = await workers.Reader.evaluate(tabId => chrome.scripting.executeScript({target: {tabId}, func: () =>
      chrome.runtime.sendMessage({type: 'echo-connection-save', connection: {url: 'http://127.0.0.1:8040', key: ''}})}), tabId);
    assert(denied[0].result.error.includes('只能在插件设置'), 'page content scripts cannot redirect authenticated traffic');
    const unsafe = await popups.Reader.evaluate(async () => {
      for (const url of ['file:///secret', 'http://user:password@host', 'https://host/path', 'https://host/?key=secret']) {
        try { EchoConnection.normalize({url, key: 'fixture-key-' + 'x'.repeat(32)}); return false; } catch {}
      }
      return true;
    });
    assert(unsafe, 'credentials, paths and query secrets are rejected');
    assert(received.some(req => req.route === '/api/reader/gloss' && req.authorized));
    assert(received.some(req => req.route === '/api/learned' && req.authorized));
    assert(!await page.content().then(text => text.includes(key)), 'key never reaches webpage DOM');
    // Stop the actual MV3 worker through CDP, preserving extension storage and
    // page annotations. Unlike the other fixtures this HTTP service survives it.
    const cdp = await context.newCDPSession(page);
    const versions = new Map();
    cdp.on('ServiceWorker.workerVersionUpdated', ({versions: changed}) => {
      for (const version of changed) versions.set(version.versionId, version);
    });
    await cdp.send('ServiceWorker.enable');
    for (let i = 0; ![...versions.values()].some(v => v.scriptURL === activeWorker.url()) && i < 40; i++) {
      await page.waitForTimeout(50);
    }
    const version = [...versions.values()].find(v => v.scriptURL === activeWorker.url());
    assert(version, 'CDP sees the real extension service worker');
    const saved = await page.locator('[data-echo-word="bank"]').elementHandle();
    await cdp.send('ServiceWorker.stopWorker', {versionId: version.versionId});
    await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    await page.locator('body').evaluate(n => n.insertAdjacentHTML('beforeend', '<p id="revived">Revived bank.</p>'));
    await page.waitForFunction(() => document.querySelector('#revived [data-echo-word="bank"] .echo-reader-zh')?.textContent === '银行');
    assert(await saved.evaluate(n => n.isConnected), 'real worker restart retains completed annotations');
    await cdp.detach();
    console.log('PASS NET-001: one MV3 popup configures both modes on one real HTTP host, authenticated SSE/learned writes, shared membership, permission denial, page-settings isolation and real MV3 worker restart');
  } finally {
    await context?.close();
    await new Promise(resolve => server.close(resolve));
    assert.equal(path.dirname(profile), path.resolve(os.tmpdir())); assert(path.basename(profile).startsWith('echo-connection-test-'));
    await fs.rm(profile, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
