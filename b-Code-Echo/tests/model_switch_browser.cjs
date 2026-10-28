// ECHO-MODEL-001: actual UI, isolated HTTP fixture, no model or user database.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '../src/echo_app/static');
  let current = 'old', loading = false, failList = false, pending, target;
  const calls = [], errors = [];
  const fixture = http.createServer(async (req, res) => {
    const route = new URL(req.url, 'http://fixture').pathname;
    const json = (value, status = 200) => {res.writeHead(status, {'Content-Type':'application/json'}); res.end(JSON.stringify(value));};
    if (route === '/api/status') return json(loading ? {state:'switching',model:current,
      model_switch:{phase:'loading',target}} : {state:'ready',model:current});
    if (route === '/api/models') return failList ? json({detail:'模型列表暂不可用'},503) :
      json({managed:true,current,models:[{name:'old',supported:true,size:5e9,context:262144},
        {name:'new',supported:true,size:7e9,context:32768},{name:'partial',supported:false,reason:'模型文件未下载完整'}]});
    if (route === '/api/model') {
      let body = ''; for await (const chunk of req) body += chunk;
      target = JSON.parse(body).name; calls.push(target); loading = true;
      pending = success => {pending = null; loading = false; if (success) current = target;
        json(success ? {current} : {detail:'加载失败，已恢复原模型'},success ? 200 : 503);};
      return;
    }
    if (route.startsWith('/api/')) return json([]);
    const filename = route === '/' ? 'index.html' : path.basename(route);
    const types = {'.html':'text/html','.js':'application/javascript','.css':'text/css'};
    res.setHeader('Content-Type',types[path.extname(filename)] || 'text/plain');
    try {res.end(fs.readFileSync(path.join(root,filename)));} catch {res.writeHead(404);res.end();}
  });
  let browser;
  try {
    await new Promise(resolve => fixture.listen(0,'127.0.0.1',resolve));
    browser = await chromium.launch({channel:'msedge',headless:true});
    const page = await browser.newPage();
    page.on('pageerror',error => errors.push(error.message));
    await page.goto('http://127.0.0.1:'+fixture.address().port);
    await page.waitForFunction(() => document.getElementById('engine').dataset.state === 'ready');
    assert.match(await page.locator('#model-btn').textContent(),/更换模型/);
    failList = true;
    await page.click('#model-btn');
    await page.waitForFunction(() => document.getElementById('model-menu').textContent.includes('读取失败'));
    await page.keyboard.press('Escape');
    failList = false;
    await page.click('#model-btn');
    await page.waitForSelector('.model-opt');
    assert(await page.locator('.model-opt').filter({hasText:'partial'}).isDisabled());
    assert.match(await page.locator('#model-menu').textContent(),/其他客户端/);
    assert.match(await page.locator('#model-menu').textContent(),/32k 上下文/);
    await page.locator('.model-opt').filter({hasText:'new'}).click();
    await page.waitForFunction(() => document.getElementById('engine-text').textContent === '加载新模型');
    assert(await page.locator('#model-btn').isDisabled());
    assert(await page.locator('#send').isDisabled());
    pending(true);
    await page.waitForFunction(() => document.getElementById('model-note').textContent === 'new' &&
      !document.getElementById('model-btn').disabled);
    await page.click('#model-btn');
    await page.locator('.model-opt').filter({hasText:'old'}).click();
    await page.waitForFunction(() => document.getElementById('engine-text').textContent === '加载新模型');
    pending(false);
    await page.waitForFunction(() => document.getElementById('log').textContent.includes('已恢复原模型'));
    assert.equal(await page.locator('#model-note').textContent(),'new');
    assert.deepEqual(calls,['new','old']);
    assert.deepEqual(errors,[]);
    console.log('PASS model menu, unavailable files, retry, progress, admission and failed-switch selection preservation');
  } finally {
    if (pending) pending(false);
    if (browser) await browser.close();
    await new Promise(resolve => fixture.close(resolve));
  }
})().catch(error => {console.error(error);process.exitCode=1;});
