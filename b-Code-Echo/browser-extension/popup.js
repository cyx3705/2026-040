let tab, running = false, origin;
const $ = id => document.getElementById(id);
async function rpc(message) {
  const result = await chrome.runtime.sendMessage(message);
  if (result.error) throw new Error(result.error);
  return result.data;
}
async function inject() {
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['reader-core.js', 'content.js']});
}
async function update() {
  try {
    const value = await chrome.tabs.sendMessage(tab.id, {type: 'echo-state'});
    running = value.enabled;
    $('detail').textContent = value.error || '';
  } catch { running = false; }
  $('toggle').textContent = running ? '关闭并还原本页' : '开启本页伴读';
}
$('toggle').onclick = async () => {
  $('toggle').disabled = true;
  try {
    if (!running) {
      await rpc({type: 'status'}); // Don't annotate if local service is offline.
      await inject();
    }
    const response = await chrome.tabs.sendMessage(tab.id, {type: 'echo-toggle', enabled: !running});
    if (response?.error) throw new Error(response.error);
    await update();
  } catch (error) { $('detail').textContent = `无法开启：${error.message}。请先启动 Echo，普通网页刷新后重试。`; }
  finally { $('toggle').disabled = false; }
};
$('site').onchange = async () => {
  const checked = $('site').checked;
  try {
    if (checked && !await chrome.permissions.request({origins: [origin]})) {
      $('site').checked = false; return;
    }
    const {sites = []} = await chrome.storage.local.get('sites');
    await chrome.storage.local.set({sites: checked ? [...new Set([...sites, origin])] : sites.filter(s => s !== origin)});
    if (checked && !running) $('toggle').click();
  } catch (error) { $('detail').textContent = error.message; $('site').checked = !checked; }
};
(async () => {
  [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  try {
    const url = new URL(tab.url);
    if (!/^https?:$/.test(url.protocol) || ['127.0.0.1', 'localhost'].includes(url.hostname)) throw new Error('请在普通 HTTP/HTTPS 网页使用');
    origin = `${url.origin}/*`;
    const {sites = []} = await chrome.storage.local.get('sites');
    $('site').checked = sites.includes(origin);
    await update();
  } catch (error) {
    $('detail').textContent = error.message; $('toggle').disabled = true; $('site').disabled = true;
  }
  try {
    const state = await rpc({type: 'status'});
    $('status').textContent = `本机 Echo 已连接 · ${state.model || ''}`;
  } catch { $('status').textContent = '本机 Echo 未启动，请先运行 echo-serve'; }
})();
