let tab, running = false, origin;
let connectionRequest = 0;
const $ = id => document.getElementById(id);
async function rpc(message) {
  const result = await chrome.runtime.sendMessage(message);
  if (result.error) throw new Error(result.error);
  return result.data;
}
async function connectionStatus() {
  const version = ++connectionRequest;
  try {
    const state = await rpc({type: 'status'});
    if (version !== connectionRequest) return;
    $('status').textContent = state.model ? `已连接 · ${state.model}` : '已连接';
    $('status').dataset.state = 'ok'; $('offline').textContent = '';
  } catch (error) {
    if (version !== connectionRequest) return;
    $('status').textContent = '未连接'; $('status').dataset.state = 'error';
    $('offline').textContent = `${error.message}。请启动 Echo，或在设置里检查主机地址。`;
  }
}
async function inject() {
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['reader-core.js', 'content.js']});
}
async function setEnabled(enabled) {
  if (enabled) {
    await rpc({type: 'status'}); // Don't annotate if local service is offline.
    await inject();
  }
  const response = await chrome.tabs.sendMessage(tab.id, {type: 'echo-toggle', enabled});
  if (response?.error) throw new Error(response.error);
  await update();
}
async function update() {
  try {
    const value = await chrome.tabs.sendMessage(tab.id, {type: 'echo-state'});
    running = value.enabled;
    $('detail').textContent = value.error || '';
  } catch { running = false; }
  $('toggle').textContent = running ? '关闭并还原本页' : '开启本页伴读';
  $('web-state').textContent = running ? '已开启' : '未开启';
}
$('toggle').onclick = async () => {
  $('toggle').disabled = true;
  try {
    await setEnabled(!running);
  } catch (error) { $('detail').textContent = `无法开启：${error.message}。请先启动 Echo，普通网页刷新后重试。`; }
  finally { $('toggle').disabled = false; }
};
$('auto').onchange = async () => {
  const checked = $('auto').checked;
  $('auto').disabled = true; $('site').disabled = true;
  try {
    // Request before any other await, while the checkbox's user gesture is live.
    if (checked && !await chrome.permissions.request({origins: ['http://*/*', 'https://*/*']})) {
      $('auto').checked = false;
      $('detail').textContent = '未授予所有网站权限，未开启自动伴读。';
      return;
    }
    await chrome.storage.local.set({autoEnable: checked});
    if (checked && origin && !running) {
      try { await setEnabled(true); }
      catch (error) { $('detail').textContent = `设置已保存，本页无法开启：${error.message}。请启动 Echo 后刷新。`; }
    } else {
      $('detail').textContent = checked ? '之后打开或刷新的网页将自动开启伴读。' : '已关闭全局自动开启，保留单网站设置和本页状态。';
    }
  } catch (error) { $('detail').textContent = error.message; $('auto').checked = !checked; }
  finally { $('auto').disabled = false; $('site').disabled = !origin || $('auto').checked; }
};
$('site').onchange = async () => {
  const checked = $('site').checked;
  $('site').disabled = true; $('auto').disabled = true;
  try {
    if (checked && !await chrome.permissions.request({origins: [origin]})) {
      $('site').checked = false; return;
    }
    const {sites = []} = await chrome.storage.local.get('sites');
    await chrome.storage.local.set({sites: checked ? [...new Set([...sites, origin])] : sites.filter(s => s !== origin)});
    if (checked && !running) {
      try { await setEnabled(true); }
      catch (error) { $('detail').textContent = `设置已保存，本页无法开启：${error.message}。请启动 Echo 后刷新。`; }
    }
  } catch (error) { $('detail').textContent = error.message; $('site').checked = !checked; }
  finally { $('auto').disabled = false; $('site').disabled = !origin || $('auto').checked; }
};
(async () => {
  const {sites = [], autoEnable = false} = await chrome.storage.local.get(['sites', 'autoEnable']);
  $('auto').checked = autoEnable && await chrome.permissions.contains({origins: ['http://*/*', 'https://*/*']});
  [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  try {
    const url = new URL(tab.url);
    // YouTube 页面只显示字幕段，其他页面只显示网页段。
    const youtube = url.origin === 'https://www.youtube.com';
    $('web').hidden = youtube; $('yt').hidden = !youtube;
    const connection = await EchoConnection.settings();
    if (!/^https?:$/.test(url.protocol) || ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.origin === connection.url) throw new Error('请在普通 HTTP/HTTPS 网页使用');
    origin = `${url.origin}/*`;
    $('site').checked = sites.includes(origin);
    $('site').disabled = $('auto').checked;
    await update();
  } catch (error) {
    $('detail').textContent = error.message; $('toggle').disabled = true; $('site').disabled = true;
  }
  $('auto').disabled = false;
  await connectionStatus();
})();
window.addEventListener('echo-connection-saved', () => { void connectionStatus(); void update(); });
