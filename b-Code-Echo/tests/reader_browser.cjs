/* Isolated Edge profile; tests the actual MV3 worker and isolated content world.
   Network boundary is stubbed inside this test profile; never changes user DB. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

(async () => {
  const live = process.argv.includes('--live');
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'echo-reader-test-'));
  const extension = path.join(profile, 'extension');
  await fs.cp(path.resolve(__dirname, '../browser-extension'), extension, {recursive: true});
  const manifest = JSON.parse(await fs.readFile(path.join(extension, 'manifest.json'), 'utf8'));
  // Simulate the activeTab/site grant in this disposable profile only.
  manifest.host_permissions.push('http://echo-reader.test/*');
  await fs.writeFile(path.join(extension, 'manifest.json'), JSON.stringify(manifest));
  let context;
  try {
    context = await chromium.launchPersistentContext(path.join(profile, 'profile'), {
      channel: 'msedge', headless: true,
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker', {timeout: 15000});
    if (!live) await worker.evaluate(() => {
      globalThis.mockLearned = new Set(['the']);
      globalThis.readerRequests = 0;
      globalThis.fetch = async (url, options = {}) => {
        const route = new URL(url).pathname;
        if (route === '/api/status') return Response.json({model: 'fixture'});
        if (route === '/api/learned' || route === '/api/reader/learned') {
          if (options.method === 'POST') {
            const {lemma, learned} = JSON.parse(options.body);
            if (learned) mockLearned.add(lemma); else mockLearned.delete(lemma);
            return Response.json({ok: true});
          }
          return Response.json([...mockLearned].map(lemma => ({lemma})));
        }
        if (route !== '/api/reader/gloss') return new Response('', {status: 404});
        readerRequests++;
        const {text} = JSON.parse(options.body);
        const items = [...text.matchAll(/[A-Za-z]+/g)].map(match => ({
          start: match.index, end: match.index + match[0].length, word: match[0], lemma: match[0].toLowerCase(),
          gloss: mockLearned.has(match[0].toLowerCase()) ? '' : '默认',
          state: mockLearned.has(match[0].toLowerCase()) ? 'skipped_learned' : 'pending',
        }));
        const encoder = new TextEncoder();
        return new Response(new ReadableStream({start(controller) {
          const event = value => controller.enqueue(encoder.encode(`data: ${JSON.stringify(value)}\n\n`));
          event({phase: 'dictionary', items});
          for (const item of items.filter(i => i.state !== 'skipped_learned')) {
            event({items: [{...item, state: item.lemma === 'beside' ? 'degraded' : 'normal',
              gloss: item.lemma === 'bank' ? (text.includes('loan') ? '银行' : '岸') : '词义',
              fallback_reason: item.lemma === 'beside' ? 'fixture_failure' : null}]});
          }
          event({done: true}); controller.close();
        }}), {headers: {'Content-Type': 'text/event-stream'}});
      };
    });
    await context.route('http://echo-reader.test/**', route => route.fulfill({contentType: 'text/html', body: `
      <!doctype html><html><head><meta charset="utf-8"></head><body>
      <p id="first">😀 He sat on the <b>ba</b>nk beside the river.</p>
      <p id="second">She went to the bank to get a loan.</p>
      <p><a href="/next" id="link">bank</a></p>
      <div contenteditable="true" id="edit">bank river</div><input value="bank"><code>bank</code>
      <p id="long">${'The river bank is quiet. '.repeat(55)}</p>
      </body></html>`}));
    const page = await context.newPage();
    await page.goto('http://echo-reader.test/sample');
    const original = await page.locator('body').textContent();
    const tabId = await worker.evaluate(async () => (await chrome.tabs.query({})).find(t => t.url === 'http://echo-reader.test/sample').id);
    const send = message => worker.evaluate(async ({tabId, message}) => chrome.tabs.sendMessage(tabId, message), {tabId, message});
    await worker.evaluate(async tabId => {
      await chrome.scripting.executeScript({target: {tabId}, files: ['reader-core.js', 'content.js']});
    }, tabId);
    await send({type: 'echo-toggle', enabled: true});
    try {
      await page.waitForFunction(() => document.querySelector('#second [data-echo-word="bank"]:not([data-pending]) .echo-reader-zh')?.textContent === '银行');
    } catch (error) {
      console.error(JSON.stringify({state: await send({type: 'echo-state'}),
        first: await page.locator('#first').innerHTML(), second: await page.locator('#second').innerHTML()}));
      throw error;
    }
    assert.equal(await page.locator('#first [data-echo-word="bank"]').count(), 2, 'word spans inline tags');
    assert.equal(await page.locator('#first [data-echo-word="bank"] .echo-reader-zh').textContent(), '岸');
    assert.equal(await page.locator('#edit [data-echo-word],code [data-echo-word],input [data-echo-word]').count(), 0);
    if (live) {
      const screenshot = process.argv.find(arg => arg.startsWith('--screenshot='))?.slice('--screenshot='.length);
      if (screenshot) await page.screenshot({path: screenshot, fullPage: false});
      await send({type: 'echo-toggle', enabled: false});
      assert.equal(await page.locator('body').textContent(), original);
      console.log('PASS live Edge extension → MV3 worker → HTTP → C++ shared input KV: bank=岸/银行, editable exclusion, original restoration');
      return;
    }
    assert.equal(await page.locator('#first [data-echo-word="the"][data-known]').count(), 2);
    assert.equal(await page.locator('#first [data-echo-word="beside"][data-degraded]').count(), 1);
    const bank = page.locator('#second [data-echo-word="bank"]');
    await bank.click();
    await page.waitForFunction(() => document.querySelector('#second [data-echo-word="bank"]')?.hasAttribute('data-known'));
    assert(await worker.evaluate(() => mockLearned.has('bank')));
    await bank.click();
    await page.waitForFunction(() => document.querySelector('#second [data-echo-word="bank"]:not([data-pending]) .echo-reader-zh')?.textContent === '银行');
    assert(!await worker.evaluate(() => mockLearned.has('bank')));
    // Read original text after turning off; tags, links and input content survive.
    await send({type: 'echo-toggle', enabled: false});
    assert.equal(await page.locator('[data-echo-word]').count(), 0);
    assert.equal(await page.locator('body').textContent(), original);
    assert.equal(await page.locator('#first b').textContent(), 'ba');
    assert.equal(await page.locator('#link').getAttribute('href'), '/next');
    // Dynamic paragraphs and repeated enable/disable do not recursively annotate.
    await send({type: 'echo-toggle', enabled: true});
    await page.evaluate(() => {
      const p = document.createElement('p'); p.id = 'dynamic'; p.textContent = 'The bank gave a loan.';
      document.body.prepend(p);
    });
    await page.waitForFunction(() => document.querySelector('#dynamic [data-echo-word="bank"] .echo-reader-zh')?.textContent === '银行');
    assert(await worker.evaluate(() => readerRequests < 35), 'bounded requests, no self-mutation loop');
    await send({type: 'echo-toggle', enabled: false});
    console.log('PASS real MV3 worker/content: streaming, learned reversal, inline UTF-16 ranges, dynamic DOM, editable exclusion and restoration');
  } finally {
    await context?.close();
    assert.equal(path.dirname(profile), path.resolve(os.tmpdir()));
    assert(path.basename(profile).startsWith('echo-reader-test-'));
    await fs.rm(profile, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
