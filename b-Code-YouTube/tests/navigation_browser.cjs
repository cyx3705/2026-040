/* YT-014: cold home -> first SPA watch with delayed player and signed CC track. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'echo-yt-navigation-'));
  let context;
  try {
    const extension = path.resolve(__dirname, '../../b-Code-Reader/extension');
    context = await chromium.launchPersistentContext(profile, {channel:'msedge', headless:true,
      args:[`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]});
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    await worker.evaluate(() => {
      globalThis.calls = [];
      globalThis.fetch = async (url, options={}) => {
        if (url.endsWith('/api/reader/learned')) return Response.json([]);
        if (url.endsWith('/api/status')) return Response.json({model:'fixture'});
        const {text} = JSON.parse(options.body); calls.push(text);
        const items = [...text.matchAll(/[A-Za-z]+/g)].map(m => ({start:m.index,end:m.index+m[0].length,
          word:m[0],lemma:m[0].toLowerCase(),gloss:'中文',state:'normal'}));
        return new Response(`data: ${JSON.stringify({phase:'dictionary',items})}\n\ndata: {"done":true}\n\n`);
      };
    });
    await context.route('https://www.youtube.com/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/timedtext') return route.fulfill({contentType:'application/json',
        body:url.searchParams.has('pot') ? JSON.stringify({events:[{tStartMs:0,dDurationMs:6000,
          segs:[{utf8:'The river bank.'}]}]}) : ''});
      return route.fulfill({contentType:'text/html',body:`<!doctype html><html><body>
        <a id="open" href="/watch?v=cold">Open video</a><script>
        document.querySelector('#open').onclick = event => {
          event.preventDefault(); history.pushState({}, '', '/watch?v=cold');
          document.dispatchEvent(new Event('yt-navigate-finish'));
          setTimeout(() => {
            const player=document.createElement('div');player.id='movie_player';player.className='html5-video-player';
            player.style.cssText='position:relative;width:960px;height:540px';
            player.innerHTML='<video></video><button class="ytp-subtitles-button" aria-pressed="false">CC</button>';
            document.body.append(player);
            setTimeout(() => {
              let on=false, selected=null;
              player.isSubtitlesOn=()=>on;
              player.getOption=(module,key)=>key==='track'?selected:[];
              player.getPlayerResponse=()=>({videoDetails:{videoId:'cold'},captions:{playerCaptionsTracklistRenderer:{
                captionTracks:[{languageCode:'en',vssId:'.en',baseUrl:location.origin+'/api/timedtext?v=cold&lang=en'}]}}});
              player.loadModule=()=>{};player.setOption=(module,key,track)=>{selected=track;};
              player.toggleSubtitles=()=>{
                on=!on; player.querySelector('button').setAttribute('aria-pressed',String(on));
                if(on) fetch('/api/timedtext?v=cold&lang=en&pot=fixture');
              };
              document.dispatchEvent(new Event('yt-player-updated'));
            },350);
          },200);
        };
        </script></body></html>`});
    });
    const page = await context.newPage();
    await page.goto('https://www.youtube.com/');
    await page.waitForTimeout(200);
    await page.locator('#open').click();
    await page.waitForFunction(() => document.querySelector('[data-echo-youtube]')?.shadowRoot
      .querySelector('[data-lemma="bank"] .zh')?.textContent === '中文', null, {timeout:9000});
    assert.equal(await page.locator('#movie_player .ytp-subtitles-button').getAttribute('aria-pressed'),'true');
    assert.equal(await worker.evaluate(() => calls.length),1);
    console.log('PASS YT-014: first home navigation, delayed player API, empty unsigned track, automatic English CC, overlay and Chinese without reload');
  } finally {
    await context?.close();
    assert.equal(path.dirname(profile),path.resolve(os.tmpdir()));
    assert(path.basename(profile).startsWith('echo-yt-navigation-'));
    await fs.rm(profile,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
