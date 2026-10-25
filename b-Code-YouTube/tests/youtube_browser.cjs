/* Isolated real MV3 workers, page MAIN bridge, media clock and shadow overlay.
   Website/timing fixtures are local; --live uses Echo read-only, never marks. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const live = process.argv.includes('--live');
  const upgrade = process.argv.includes('--upgrade');
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'echo-youtube-test-'));
  let context;
  try {
    const reader = path.join(profile, 'reader');
    await fs.cp(path.resolve(__dirname, '../../b-Code-Reader/extension'), reader, {recursive: true});
    if (upgrade) {
      // Model a running pre-merge extension: reader-only manifest plus the all-web
      // grant already held by existing users. No static caption scripts exist.
      const file = path.join(reader, 'manifest.json');
      const manifest = JSON.parse(await fs.readFile(file, 'utf8'));
      delete manifest.content_scripts;
      manifest.host_permissions = ['http://127.0.0.1:8040/*', 'https://*/*'];
      await fs.writeFile(file, JSON.stringify(manifest));
      // Keep the old reader worker's protocol boundary: it knows only the shared
      // reader port and canonical status/learned/set-learned operations. This
      // catches the mixed-version state missed by the previous manifest-only test.
      const background = path.join(reader, 'background.js');
      const legacyBoundary = `
        const addPort = chrome.runtime.onConnect.addListener.bind(chrome.runtime.onConnect);
        chrome.runtime.onConnect.addListener = listener => addPort(port => {
          if (port.name === 'echo-reader') listener(port);
        });
        const addMessage = chrome.runtime.onMessage.addListener.bind(chrome.runtime.onMessage);
        chrome.runtime.onMessage.addListener = listener => addMessage((message, sender, reply) => {
          if (message.type.startsWith('echo-youtube-')) { reply({error: '未知伴读操作'}); return; }
          return listener(message, sender, reply);
        });
      `;
      await fs.writeFile(background, legacyBoundary + await fs.readFile(background, 'utf8'));

    }
    context = await chromium.launchPersistentContext(path.join(profile, 'profile'), {
      channel: 'msedge', headless: true, viewport: {width: 1100, height: 760},
      args: [`--disable-extensions-except=${reader}`, `--load-extension=${reader}`],
    });
    const youtubeWorker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    const readerWorker = youtubeWorker;
    assert.equal(context.serviceWorkers().length, 1, 'one extension owns both reading modes');
    for (const worker of [youtubeWorker]) {
      if (!live) await worker.evaluate(() => {
        globalThis.glossRequests = []; globalThis.mockLearned = new Set(['the']); globalThis.delay = 1200; globalThis.rejectLearned = false; globalThis.holdNextLearned = false;
        globalThis.fetch = async (url, options = {}) => {
          const route = new URL(url).pathname;
          if (route === '/api/status') return Response.json({model: 'fixture'});
          if (route === '/api/reader/learned' && rejectLearned) throw new Error('fixture learning-state connection failed');
          if (route === '/api/reader/learned') {
            const snapshot = [...mockLearned].map(lemma => ({lemma}));
            if (holdNextLearned) {
              holdNextLearned = false;
              return new Promise(resolve => { globalThis.releaseLearned = () => resolve(Response.json(snapshot)); });
            }
            return Response.json(snapshot);
          }
          if (route === '/api/learned') {
            const {lemma, learned} = JSON.parse(options.body);
            if (learned) mockLearned.add(lemma); else mockLearned.delete(lemma);
            return Response.json({ok: true});
          }
          if (route !== '/api/reader/gloss') throw new Error(`Unexpected API ${route}`);
          const {text, session} = JSON.parse(options.body), record = {text, session, aborted: false}; glossRequests.push(record);
          const items = [...text.matchAll(/[A-Za-z]+/g)].map(match => ({
            start: match.index, end: match.index + match[0].length, word: match[0], lemma: match[0].toLowerCase(),
            gloss: mockLearned.has(match[0].toLowerCase()) ? '' : '默认',
            state: mockLearned.has(match[0].toLowerCase()) ? 'skipped_learned' : 'pending',
          }));
          const encoder = new TextEncoder(); let timer;
          return new Response(new ReadableStream({start(controller) {
            const event = value => controller.enqueue(encoder.encode(`data: ${JSON.stringify(value)}\n\n`));
            event({phase: 'dictionary', items});
            timer = setTimeout(() => {
              for (const item of items.filter(item => item.state !== 'skipped_learned')) event({items: [{...item,
                state: item.lemma === 'quiet' ? 'degraded' : 'normal',
                gloss: item.lemma === 'bank' ? (text.includes('loan') ? '银行' : '岸') : '词义',
                fallback_reason: item.lemma === 'quiet' ? 'fixture_failure' : null}]});
              event({done: true}); controller.close();
            }, delay);
            options.signal.addEventListener('abort', () => {
              record.aborted = true; clearTimeout(timer); controller.error(new DOMException('Cancelled', 'AbortError'));
            }, {once: true});
          }, cancel() { clearTimeout(timer); }}), {headers: {'Content-Type': 'text/event-stream'}});
        };
      });
    }
    let timedRequests = 0;
    await context.route('https://www.youtube.com/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/timedtext') {
        timedRequests++;
        if (url.searchParams.get('source') === 'xhr') return route.fulfill({contentType: 'text/xml',
          body: '<?xml version="1.0"?><timedtext><body><p t="1000" d="3000"><s>XML bank.</s></p></body></timedtext>'});
        // Like the real site, a track URL without a fresh playback token returns an empty body.
        if (url.searchParams.get('v') === 'race' && !url.searchParams.has('pot')) return route.fulfill({contentType: 'application/json', body: ''});
        const lines = url.searchParams.get('v') === 'race' ? [[1000, 3000, 'Race caption.']] : url.searchParams.get('v') === 'fixture' ?
          [[1000, 3000, '😀 The river bank is quiet.'], [4000, 3000, 'She went to the bank for a loan.'],
            [10000, 3000, 'A bright light.'], [13000, 3000, 'A bright light.'], [16000, 3000, 'Old pending work.']] :
          [[1000, 3000, 'Next caption.']];
        return route.fulfill({contentType: 'application/json', body: JSON.stringify({events: lines.map(([tStartMs, dDurationMs, text]) =>
          ({tStartMs, dDurationMs, segs: [{utf8: text}]}))})});
      }
      return route.fulfill({contentType: 'text/html', body: `<!doctype html><html><head><meta charset="utf-8"><style>
      body{background:#f4f1ea;font:18px system-ui}#movie_player{position:relative;width:960px;height:540px;background:linear-gradient(130deg,#162b3b,#10151f);margin:20px auto}
      video{width:100%;height:100%}.ytp-caption-window-container{position:absolute;bottom:12%;left:15%;color:white}
      </style></head><body><div id="movie_player" class="html5-video-player"><video></video>
      <div class="ytp-caption-window-container"><span class="ytp-caption-segment"></span></div></div>
      <p id="description">The bank gave a loan.</p><script>
      const player=document.getElementById('movie_player');
      document.querySelector('video').currentTime=.7;
      player.getPlayerResponse=()=>({videoDetails:{videoId:new URL(location.href).searchParams.get('v')||location.pathname.split('/').pop()},
      captions:{playerCaptionsTracklistRenderer:{captionTracks:[{languageCode:'en',vssId:'.en',baseUrl:location.origin+'/api/timedtext?lang=en&v='+(new URL(location.href).searchParams.get('v')||location.pathname.split('/').pop())}]}}});
      </script></body></html>`});
    });
    const page = await context.newPage();
    await page.goto('https://www.youtube.com/watch?v=fixture');
    const tabId = await readerWorker.evaluate(async () => (await chrome.tabs.query({})).find(tab => tab.url?.includes('v=fixture')).id);
    if (upgrade) {
      const missing = await youtubeWorker.evaluate(async tabId => {
        try { return await chrome.tabs.sendMessage(tabId, {type: 'echo-youtube-state'}); }
        catch { return null; }
      }, tabId);
      assert(!missing, 'pre-merge manifest leaves an existing video without a caption receiver');
      const rescue = await context.newPage();
      await rescue.addInitScript(id => {
        chrome.tabs.query = async () => [await chrome.tabs.get(id)];
      }, tabId);
      await rescue.goto(new URL('popup.html', youtubeWorker.url()).href);
      await rescue.locator('#refresh').click();
      // Caption acquisition is asynchronous; check the actual page state first,
      // then refresh the preview without resetting the newly bootstrapped bridge.
      let bootstrapped;
      for (let i = 0; i < 100; i++) {
        bootstrapped = await youtubeWorker.evaluate(tabId => chrome.tabs.sendMessage(tabId, {type: 'echo-youtube-state'}), tabId);
        if (bootstrapped.source === 'timeline') break;
        await rescue.waitForTimeout(50);
      }
      assert.equal(bootstrapped.source, 'timeline', 'popup installs and starts both script worlds');
      await rescue.evaluate(() => preview());
      assert((await rescue.locator('#mode').textContent()).includes('时间轴预取'));
      await page.evaluate(() => { globalThis.fixtureBridgeFetch = window.fetch; });
      await rescue.evaluate(() => preview());
      assert(await page.evaluate(() => window.fetch === fixtureBridgeFetch), 'reopening popup does not wrap native fetch twice');
      await rescue.evaluate(async () => {
        const original = chrome.scripting.executeScript;
        chrome.scripting.executeScript = async () => { throw new Error('fixture site access denied'); };
        try { await preview(true); } finally { chrome.scripting.executeScript = original; }
      });
      assert((await rescue.locator('#caption').textContent()).includes('fixture site access denied'), 'injection failure has a concrete visible error');
      await rescue.evaluate(() => preview());

      await rescue.close(); await page.bringToFront();
    }
    const state = () => youtubeWorker.evaluate(tabId => chrome.tabs.sendMessage(tabId, {type: 'echo-youtube-state'}), tabId);
    const clock = time => page.locator('video').evaluate((node, time) => { node.currentTime = time; node.dispatchEvent(new Event('timeupdate')); }, time);
    const slot = role => page.locator(`[data-echo-youtube] .line[data-role="${role}"]`);
    const token = (word, role = 'current') => slot(role).locator('.token').filter({has: page.locator('.en', {hasText: new RegExp(`^${word}$`, 'i')})}).first();
    const rows = () => page.locator('[data-echo-youtube] .line').evaluateAll(nodes => nodes.map(node => ({
      role: node.dataset.role, words: [...node.querySelectorAll('.en')].map(word => word.textContent).join(' '),
    })));
    const waitState = async predicate => {
      for (let i = 0; i < 100; i++) { const value = await state(); if (predicate(value)) return value; await new Promise(resolve => setTimeout(resolve, 50)); }
      assert.fail(`Unexpected subtitle state ${JSON.stringify(await state())}`);
    };
    await waitState(value => value.source === 'timeline' && (live ? value.cached > 0 : value.pending > 0));
    assert.equal((await state()).caption, '', 'future English is not displayed early');
    assert(timedRequests > 0, 'MAIN bridge fetched a real timestamped track through the page session');
    if (!live) {
      await youtubeWorker.evaluate(() => { rejectLearned = true; });
      await youtubeWorker.evaluate(tabId => chrome.tabs.sendMessage(tabId, {type: 'echo-youtube-retry'}), tabId);
      await waitState(value => value.source === 'timeline' && value.error.includes('fixture learning-state'));
      await youtubeWorker.evaluate(() => { rejectLearned = false; });
      await youtubeWorker.evaluate(tabId => chrome.tabs.sendMessage(tabId, {type: 'echo-youtube-retry'}), tabId);
      await waitState(value => value.source === 'timeline' && value.pending > 0 && !value.error);
    }
    await clock(1.02);
    await page.locator('[data-echo-youtube] .token:not(:disabled)').first().waitFor();
    const bounds = () => page.locator('[data-echo-youtube] .panel').evaluate(node => {
      const rect = node.getBoundingClientRect(); return {width: rect.width, height: rect.height};
    });
    const before = await bounds();
    assert.deepEqual((await rows()).map(row => row.role), ['empty', 'current'], 'first cue starts in the lower reserved slot');
    const initialRowBounds = await page.locator('[data-echo-youtube] .line').evaluateAll(nodes => nodes.map(node => {
      const rect = node.getBoundingClientRect(); return {top: rect.top, height: rect.height};
    }));
    assert.equal(initialRowBounds[0].height, initialRowBounds[1].height, 'both physical slots are reserved from the start');
    if (!live) {
      assert.equal(await token('bank').locator('.zh').textContent(), '默认');
      await page.waitForFunction(() => [...document.querySelector('[data-echo-youtube]')?.shadowRoot.querySelectorAll('.token') || []]
        .some(node => node.dataset.lemma === 'bank' && node.querySelector('.zh').textContent === '岸'));
      const completedCalls = await youtubeWorker.evaluate(() => glossRequests.length);
      const completedRows = await rows();
      await youtubeWorker.evaluate(() => {
        for (const port of [...ports]) { cancel(port); ports.delete(port); port.disconnect(); }
      });
      await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
      await page.waitForTimeout(200);
      assert.deepEqual(await rows(), completedRows, 'idle reconnect retains both physical subtitle slots');
      assert.equal(await youtubeWorker.evaluate(() => glossRequests.length), completedCalls, 'completed subtitle is not recomputed');
      assert.deepEqual(await bounds(), before, 'dictionary/semantic fill does not move reserved rows');
      assert.equal(await token('the').locator('.zh').textContent(), '', 'learned words keep an empty reserved Chinese slot');
      assert.equal(await token('quiet').locator('.zh').evaluate(node => getComputedStyle(node).textDecorationStyle), 'wavy');
      const beforeBankLearning = await youtubeWorker.evaluate(() => glossRequests.length);
      await youtubeWorker.evaluate(() => { holdNextLearned = true; });
      await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
      for (let i = 0; !await youtubeWorker.evaluate(() => Boolean(globalThis.releaseLearned)) && i < 50; i++) await page.waitForTimeout(20);

      await token('bank').click();
      await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot.querySelector('[data-lemma="bank"][data-known]'));
      assert(await youtubeWorker.evaluate(() => mockLearned.has('bank')));
      await youtubeWorker.evaluate(() => releaseLearned());
      await page.waitForTimeout(100);
      assert(await token('bank').evaluate(node => node.hasAttribute('data-known')), 'stale subtitle membership GET cannot undo the latest click');

      assert.deepEqual(await bounds(), before, 'hiding learned gloss does not shrink rows');
      await token('bank').click();
      await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot.querySelector('[data-lemma="bank"] .zh')?.textContent === '岸');
      assert.equal(await youtubeWorker.evaluate(() => glossRequests.length), beforeBankLearning, 'marking/restoring cached subtitle glosses makes no translation request');
    } else {
      await waitState(value => value.pending === 0);
      const gloss = await token('bank').locator('.zh').textContent();
      assert(/岸/.test(gloss) || await token('bank').evaluate(node => node.hasAttribute('data-known')), 'live shared-KV river sense or saved learned skip');
    }
    const screenshot = process.argv.find(arg => arg.startsWith('--screenshot='))?.slice(13);
    await readerWorker.evaluate(async tabId => {
      await chrome.scripting.executeScript({target: {tabId}, files: ['reader-core.js', 'content.js']});
      await chrome.tabs.sendMessage(tabId, {type: 'echo-toggle', enabled: true});
    }, tabId);
    await page.waitForSelector('#description [data-echo-word="bank"]');
    assert.equal(await page.locator('#movie_player [data-echo-word]').count(), 0, 'web reader skips the player');
    await page.locator('#movie_player').evaluate(node => node.classList.add('ad-showing'));
    await waitState(value => value.source === 'ad');
    assert(await page.locator('[data-echo-youtube]').first().evaluate(node => node.hidden), 'video subtitles are hidden during ads');
    await page.locator('#movie_player').evaluate(node => node.classList.remove('ad-showing'));
    await waitState(value => value.caption.includes('river'));
    await clock(3.7);
    await waitState(value => value.pending > 0 || value.cached >= 2);
    assert((await state()).caption.includes('river'), 'next cue prefetch retains current English');
    if (!live) {
      await youtubeWorker.evaluate(() => {
        for (const port of [...ports]) { cancel(port); ports.delete(port); port.disconnect(); }
      });
      await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    }

    await clock(4.02);
    await waitState(value => value.caption.includes('loan'));
    assert.deepEqual((await rows()).map(row => row.role), ['current', 'history'], 'second cue replaces upper slot; lower stays history');
    assert((await rows())[1].words.includes('river'), 'previous cue stays in its physical slot');
    assert.deepEqual(await page.locator('[data-echo-youtube] .line').evaluateAll(nodes => nodes.map(node => {
      const rect = node.getBoundingClientRect(); return {top: rect.top, height: rect.height};
    })), initialRowBounds, 'short cue transitions keep both reserved physical positions');
    await waitState(value => value.pending === 0);
    const bank = await token('bank').locator('.zh').textContent();
    assert(/银行/.test(bank) || live && await token('bank').evaluate(node => node.hasAttribute('data-known')), 'loan sense selected in its own cue');
    if (!live) assert(await youtubeWorker.evaluate(() => glossRequests.some(record => record.text.includes('loan'))));
    if (!live) assert(await youtubeWorker.evaluate(() => glossRequests.filter(record => /river|She went/.test(record.text)).every(record => record.session === 'yt:fixture') &&
      glossRequests.filter(record => record.text.includes('gave a loan')).every(record => record.session === 'web:https://www.youtube.com/watch')),
      'KV-001: cues use their video context, page text its page context');
    if (screenshot) await page.screenshot({path: screenshot});
    if (!live) {
      const beforeHistoryLearning = await youtubeWorker.evaluate(() => glossRequests.length);
      await token('bank', 'history').click();
      await page.waitForFunction(() => [...document.querySelector('[data-echo-youtube]').shadowRoot.querySelectorAll('[data-lemma="bank"]')]
        .every(node => node.hasAttribute('data-known') && node.querySelector('.zh').textContent === ''));
      assert.deepEqual((await rows()).map(row => row.role), ['current', 'history'], 'history learning updates both rows without rotating');
      await token('bank', 'history').click();
      await page.waitForFunction(() => [...document.querySelector('[data-echo-youtube]').shadowRoot.querySelectorAll('[data-lemma="bank"] .zh')]
        .every(node => /岸|银行/.test(node.textContent)));
      assert.equal(await youtubeWorker.evaluate(() => glossRequests.length), beforeHistoryLearning, 'history membership changes retain cached per-occurrence senses');
    }
    await clock(8);
    await waitState(value => value.caption.includes('loan'));
    assert(!await page.locator('[data-echo-youtube]').first().evaluate(node => node.hidden), 'caption gap keeps both existing rows visible');
    const gapRows = await rows();
    assert.deepEqual(gapRows.map(row => row.role), ['current', 'history'], 'cue end does not rotate or empty either row');
    assert(await page.locator('#movie_player').getAttribute('data-echo-youtube-active') !== null, 'held overlay keeps native captions hidden');
    await clock(9.7);
    await waitState(value => value.pending > 0 || value.cached >= 3);
    assert((await state()).caption.includes('loan'), 'prefetch keeps previous English visible during the gap');
    assert.deepEqual(await rows(), gapRows, 'future prefetch cannot replace held rows early');
    await clock(10.02);
    await waitState(value => value.caption === 'A bright light.');
    assert.deepEqual((await rows()).map(row => row.role), ['history', 'current'], 'third cue replaces lower slot; upper stays history');
    assert((await rows())[0].words.includes('loan'));
    if (!live) {
      await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot
        .querySelector('[data-role="history"] [data-lemma="bank"] .zh')?.textContent === '银行');
      assert(await youtubeWorker.evaluate(() => glossRequests.filter(record => record.text.includes('loan')).some(record => !record.aborted)), 'history results can complete after the next cue');
    }
    await clock(13.02);
    await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot
      .querySelector('[data-slot="0"][data-role="current"] .en')?.textContent === 'A');
    assert.deepEqual((await rows()).map(row => row.words), ['A bright light', 'A bright light'], 'repeated identical text still alternates distinct occurrences');
    await waitState(value => value.pending === 0);
    if (!live) {
      await token('light').click();
      await page.waitForFunction(() => [...document.querySelector('[data-echo-youtube]').shadowRoot.querySelectorAll('[data-lemma="light"]')]
        .every(node => node.hasAttribute('data-known')));
      await token('light').click();
      await page.waitForFunction(() => [...document.querySelector('[data-echo-youtube]').shadowRoot.querySelectorAll('[data-lemma="light"] .zh')]
        .every(node => node.textContent === '词义'));
    }
    await page.locator('video').evaluate(node => node.dispatchEvent(new Event('seeking')));
    await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot.querySelector('[data-slot="0"][data-role="empty"]'));
    assert.deepEqual((await rows()).map(row => row.role), ['empty', 'current'], 'explicit seek clears populated history');
    await clock(10.1);
    await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot.querySelector('[data-slot="0"][data-role="empty"]'));
    assert.deepEqual((await rows()).map(row => row.role), ['empty', 'current'], 'backward seek clears future history');
    await clock(1.1);
    await waitState(value => value.caption.includes('river'));
    await clock(10.1);
    await waitState(value => value.caption === 'A bright light.');
    assert.deepEqual((await rows()).map(row => row.role), ['empty', 'current'], 'non-adjacent forward jump clears unrelated history');
    if (!live) {
      await clock(15.7);
      await waitState(value => value.pending > 0);
      await page.evaluate(() => {
        history.pushState({}, '', '/shorts/next-video'); document.querySelector('video').currentTime = .7;
      });
      await waitState(value => value.source === 'timeline' && value.cached <= 1);
      await clock(1.05);
      await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot.textContent.includes('Next'));
      assert(!(await rows()).some(row => row.words.includes('bright')), 'old video cannot paint either new slot');
      assert(await youtubeWorker.evaluate(() => glossRequests.some(record => record.text.includes('Old pending') && record.aborted)), 'seeking/navigation aborts stale work');
      await clock(20);
      await waitState(value => value.caption === 'Next caption.');
      assert(!await page.locator('[data-echo-youtube]').first().evaluate(node => node.hidden), 'last cue stays visible far beyond its end timestamp');
      assert.deepEqual((await rows()).map(row => row.role), ['empty', 'current'], 'holding the final cue does not rotate it again');
      await page.evaluate(() => {
        history.pushState({}, '', '/watch?v=fallback');
        document.querySelector('video').currentTime = 1.05;
        document.querySelector('#movie_player').getPlayerResponse = () => ({videoDetails: {videoId: 'fallback'}});
        document.querySelector('.ytp-caption-segment').textContent = 'River bank.';
      });
      await waitState(value => value.source !== 'timeline');
      // A SPA can briefly retain the previous caption; wait for a new native
      // text mutation before accepting it as belonging to the new video.
      await page.locator('.ytp-caption-segment').evaluate(node => { node.textContent = 'The river bank.'; });
      await waitState(value => value.source === 'live' && value.caption.includes('river'));
      await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot.querySelector('[data-lemma="bank"] .zh')?.textContent === '岸');
      const liveRows = await rows();
      await page.locator('.ytp-caption-segment').evaluate(node => { node.textContent = ''; });
      // Wait for the regular content-script probe, not just the page mutation.
      await page.waitForTimeout(200);
      assert.equal((await state()).caption, 'The river bank.', 'live DOM caption removal holds the last subtitle');
      assert.deepEqual(await rows(), liveRows, 'live gap keeps slot roles and words');
      assert(!await page.locator('[data-echo-youtube]').first().evaluate(node => node.hidden));
      await page.locator('.ytp-caption-segment').evaluate(node => { node.textContent = 'Another caption.'; });
      await waitState(value => value.caption === 'Another caption.');
      assert.deepEqual((await rows()).map(row => row.role), ['current', 'history'], 'new live subtitle rotates after a held gap');
      await page.evaluate(async () => {
        const response = await fetch('/api/timedtext?v=fallback&lang=en&pot=fixture');
        if (!(await response.text()).includes('Next caption')) throw new Error('Native fetch response was altered');
      });
      await waitState(value => value.source === 'timeline' && value.caption === 'Next caption.');
      await page.evaluate(() => new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest(); xhr.open('GET', '/api/timedtext?v=fallback&lang=en&source=xhr&pot=fixture');
        xhr.onload = () => xhr.responseText.includes('XML bank') ? resolve() : reject(new Error('Native XHR response was altered'));
        xhr.onerror = reject; xhr.send();
      }));
      await waitState(value => value.caption === 'XML bank.');
      await page.evaluate(() => {
        const track = document.querySelector('video').addTextTrack('subtitles', 'English', 'en');
        track.mode = 'hidden'; track.addCue(new VTTCue(1, 4, 'Native text track.'));
      });
      await waitState(value => value.caption === 'Native text track.');
      // A short silent WAV drives a genuine native HTMLMediaElement ended
      // state, shared across MAIN/isolated worlds, without getter mocks.
      const audio = Buffer.alloc(44 + 16000 * 2 * 6);
      audio.write('RIFF'); audio.writeUInt32LE(audio.length - 8, 4); audio.write('WAVEfmt ', 8);
      audio.writeUInt32LE(16, 16); audio.writeUInt16LE(1, 20); audio.writeUInt16LE(1, 22);
      audio.writeUInt32LE(16000, 24); audio.writeUInt32LE(32000, 28);
      audio.writeUInt16LE(2, 32); audio.writeUInt16LE(16, 34);
      audio.write('data', 36); audio.writeUInt32LE(audio.length - 44, 40);
      await page.locator('video').evaluate((node, data) => { node.muted = true; node.src = data; }, `data:audio/wav;base64,${audio.toString('base64')}`);
      await page.waitForFunction(() => document.querySelector('video').readyState >= 1);
      await clock(5.8);
      await waitState(value => value.caption === 'Native text track.');
      assert(!await page.locator('[data-echo-youtube]').first().evaluate(node => node.hidden), 'final cue remains during the tail of actual media');
      await page.locator('video').evaluate(node => node.play());
      await page.waitForFunction(() => document.querySelector('video').ended);
      await waitState(value => value.caption === '');
      assert(await page.locator('[data-echo-youtube]').first().evaluate(node => node.hidden), 'playback end hides held captions');
      assert.equal(await page.locator('#movie_player').getAttribute('data-echo-youtube-active'), null);
      await clock(2);
      await waitState(value => value.caption === 'Native text track.');
      assert(!await page.locator('[data-echo-youtube]').first().evaluate(node => node.hidden), 'seeking back after end restores the appropriate cue');
    }
    // The upgrade model has no static content scripts, so a reloaded page has no receiver.
    if (!live && !upgrade) {
      // YT-009: finished glosses survive closing the page; reopening resumes without recomputing.
      // The live cue finished moments ago, so only the pagehide write can have stored it.
      await waitState(value => value.saved > 0);
      await page.goto('https://www.youtube.com/watch?v=fixture');
      const stored = await youtubeWorker.evaluate(async () => chrome.storage.local.get(null));
      assert(stored['echoYouTubeGlosses:fixture']?.texts['She went to the bank for a loan.']?.some(item => item.lemma === 'bank' && item.gloss === '银行'), 'finished cue saved for its video');
      assert(stored['echoYouTubeGlosses:fallback']?.texts['The river bank.'], 'closing the page writes the latest finished cue');
      assert(['fixture', 'fallback'].every(id => stored.echoYouTubeGlossIndex.includes(id)), 'saved videos are indexed for pruning');
      await waitState(value => value.enabled && value.saved > 0 && value.source === 'timeline');
      const beforeReopen = await youtubeWorker.evaluate(() => glossRequests.filter(record => record.text.includes('loan')).length);
      await clock(4.02);
      await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot.querySelector('[data-lemma="bank"] .zh')?.textContent === '银行');
      await page.waitForTimeout(200);
      assert.equal(await youtubeWorker.evaluate(() => glossRequests.filter(record => record.text.includes('loan')).length), beforeReopen,
        'reopened video uses saved glosses instead of a new request');
      // YT-010: the player fetches the next video's track before the URL changes
      // and a direct fetch has no playback token; the captured track must still arrive.
      await page.evaluate(async () => {
        await fetch('/api/timedtext?v=race&lang=en&pot=fixture');
        history.pushState({}, '', '/watch?v=race'); document.querySelector('video').currentTime = .7;
      });
      await waitState(value => value.source === 'timeline');
      await clock(1.05);
      await waitState(value => value.caption === 'Race caption.');
    }
    await youtubeWorker.evaluate(() => chrome.storage.local.set({enabled: false}));
    await waitState(value => !value.enabled);
    assert.equal(await page.locator('[data-echo-youtube]').count(), 0, 'disable removes overlay and owned style');
    assert(!await page.locator('#movie_player').getAttribute('data-echo-youtube-active'));
    const popup = await context.newPage();
    await popup.goto(new URL('popup.html', youtubeWorker.url()).href);
    await popup.waitForFunction(() => document.getElementById('status').textContent.includes('已连接'));
    assert.equal(await popup.locator('#enabled').isChecked(), false);
    await popup.locator('summary').click();
    await popup.locator('#lead').selectOption('24');
    assert.equal(await youtubeWorker.evaluate(async () => (await chrome.storage.local.get('leadFrames')).leadFrames), 24);
    if (upgrade) console.log('PASS YT-007/008: pre-merge manifest + reader-only background/no caption receiver → popup installs MAIN and isolated scripts on the existing video without reload');
    console.log(live ? 'PASS LIVE Echo/C++: timed prefill, bank=岸/银行 or saved learned skip, bilingual overlay, shared service, no learned writes' :
      'PASS YouTube fixture: MAIN/fetch/XHR/XML/textTracks, alternating current/history slots, late history glosses, repeated cues, reserved rows, learned reversal, held timeline/live gaps and final cue, native media end/replay, seek/SPA cancellation, saved glosses across reopen, early-captured SPA track, coexistence and settings');
  } finally {
    await context?.close();
    assert.equal(path.dirname(profile), path.resolve(os.tmpdir())); assert(path.basename(profile).startsWith('echo-youtube-test-'));
    await fs.rm(profile, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
