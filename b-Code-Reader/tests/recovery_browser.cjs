/* WEB-007: real MV3 port loss, truncated SSE and offline recovery in a disposable profile. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'echo-recovery-test-'));
  let context;
  try {
    const root = path.join(profile, 'extension');
    await fs.cp(path.resolve(__dirname, '../extension'), root, {recursive: true});
    const manifest = JSON.parse(await fs.readFile(path.join(root, 'manifest.json'), 'utf8'));
    manifest.host_permissions.push('https://learning.example/*');
    await fs.writeFile(path.join(root, 'manifest.json'), JSON.stringify(manifest));
    context = await chromium.launchPersistentContext(path.join(profile, 'profile'), {
      channel: 'msedge', headless: true,
      args: [`--disable-extensions-except=${root}`, `--load-extension=${root}`],
    });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    await worker.evaluate(() => {
      globalThis.calls = []; globalThis.mode = 'complete';
      globalThis.fetch = async (url, options = {}) => {
        const route = new URL(url).pathname;
        if (mode === 'offline') throw new Error('fixture offline');
        if (route === '/api/status') return Response.json({model: 'fixture'});
        if (route === '/api/reader/learned') return Response.json([]);
        const {text} = JSON.parse(options.body); calls.push(text);
        const items = [...text.matchAll(/[A-Za-z]+/g)].map(m => ({start: m.index, end: m.index + m[0].length,
          word: m[0], lemma: m[0].toLowerCase(), state: 'pending', gloss: '默认'}));
        const encoder = new TextEncoder();
        return new Response(new ReadableStream({start(controller) {
          const send = data => controller.enqueue(encoder.encode(`data: ${JSON.stringify(data)}\n\n`));
          send({phase: 'dictionary', items});
          if (mode === 'hold') {
            options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), {once: true});
            return;
          }
          if (mode !== 'truncated') {
            send({items: items.map(item => ({...item, state: 'normal', gloss: '已恢复'}))});
            send({done: true});
          }
          controller.close();
        }}));
      };
    });
    await context.route('https://learning.example/**', route => route.fulfill({contentType: 'text/html',
      body: '<!doctype html><html><body><p id="done">Completed words.</p><div id="dynamic"></div></body></html>'}));
    const page = await context.newPage(); await page.goto('https://learning.example/');
    const tabId = await worker.evaluate(async () => (await chrome.tabs.query({})).find(t => t.url?.includes('learning.example')).id);
    await worker.evaluate(async tabId => {
      await chrome.scripting.executeScript({target: {tabId}, files: ['reader-core.js', 'content.js']});
      await chrome.tabs.sendMessage(tabId, {type: 'echo-toggle', enabled: true});
    }, tabId);
    await page.waitForFunction(() => document.querySelector('#done .echo-reader-zh')?.textContent === '已恢复');
    const completed = await page.locator('#done [data-echo-word]').first().elementHandle();
    // Actual runtime.Port disconnects simulate the worker suspension boundary.
    await worker.evaluate(() => { for (const port of [...ports]) { cancel(port); ports.delete(port); port.disconnect(); } });
    await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    await worker.evaluate(() => { mode = 'hold'; });
    await page.locator('#dynamic').evaluate(n => { n.innerHTML = '<p id="broken">Interrupted bank.</p>'; });
    await page.waitForSelector('#broken [data-pending]');
    await worker.evaluate(() => { mode = 'complete'; for (const port of [...ports]) { cancel(port); ports.delete(port); port.disconnect(); } });
    await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    await page.waitForFunction(() => document.querySelector('#broken .echo-reader-zh')?.textContent === '已恢复');
    assert.equal(await page.locator('#broken [data-echo-word]').count(), 2, 'retry updates existing wrappers');
    assert(await completed.evaluate(n => n.isConnected), 'completed DOM survives reconnect');
    assert.equal(await worker.evaluate(() => calls.filter(t => t.includes('Completed')).length), 1, 'completed text is not recomputed');
    // Clean EOF without the protocol done event must be retried, too.
    await worker.evaluate(() => { mode = 'truncated'; });
    await page.locator('#dynamic').evaluate(n => n.insertAdjacentHTML('beforeend', '<p id="eof">Truncated stream.</p>'));
    await page.waitForSelector('#eof [data-degraded]');
    await worker.evaluate(() => { mode = 'complete'; });
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await page.waitForFunction(() => document.querySelector('#eof .echo-reader-zh')?.textContent === '已恢复');
    // The server can be unavailable when a returning tab wakes up.
    await worker.evaluate(() => { mode = 'offline'; });
    await page.locator('#dynamic').evaluate(n => n.insertAdjacentHTML('beforeend', '<p id="offline">Offline recovery.</p>'));
    await page.waitForTimeout(250);
    await worker.evaluate(() => { mode = 'complete'; });
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await page.waitForFunction(() => document.querySelector('#offline .echo-reader-zh')?.textContent === '已恢复');
    // Stop must cancel the retry timer and leave the original page intact.
    await worker.evaluate(() => { mode = 'hold'; });
    await page.locator('#dynamic').evaluate(n => n.insertAdjacentHTML('beforeend', '<p id="stop">Stop pending.</p>'));
    await page.waitForSelector('#stop [data-pending]');
    await worker.evaluate(tabId => chrome.tabs.sendMessage(tabId, {type: 'echo-toggle', enabled: false}), tabId);
    await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    assert.equal(await page.locator('[data-echo-word]').count(), 0);
    assert.equal(await page.locator('#stop').textContent(), 'Stop pending.');
    console.log('PASS WEB-007: MV3 disconnect, interrupted/truncated/offline retry, completed DOM reuse, no duplicate wrappers, stop cancellation');
  } finally {
    await context?.close();
    assert.equal(path.dirname(profile), path.resolve(os.tmpdir()));
    assert(path.basename(profile).startsWith('echo-recovery-test-'));
    await fs.rm(profile, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
