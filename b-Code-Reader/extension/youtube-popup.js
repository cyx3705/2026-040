// YT-007: static scripts don't reach already-open tabs after an extension upgrade.
// A popup user gesture has activeTab; repair both worlds before sending commands.
async function captionTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const url = new URL(tab?.url || 'about:blank');
  if (url.origin !== 'https://www.youtube.com') throw new Error('请在 YouTube 页面使用字幕伴读。');
  return tab;
}
async function ensureCaptions(tab) {
  // The page world can be missing even when the isolated receiver exists.
  // The bridge has its own idempotency guard, so this won't wrap fetch twice.
  await chrome.scripting.executeScript({target: {tabId: tab.id}, world: 'MAIN', files: ['youtube-player.js', 'caption-bridge.js']});
  try {
    const state = await chrome.tabs.sendMessage(tab.id, {type: 'echo-youtube-state'});
    if (state && typeof state.enabled === 'boolean') return;
  } catch { /* Existing tab has no caption receiver yet. */ }
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['youtube-player.js', 'timeline-core.js', 'youtube-content.js']});
}
async function preview(retry = false, repair = true) {
  const output = document.getElementById('caption');
  try {
    const tab = await captionTab();
    if (repair) await ensureCaptions(tab);
    const state = await chrome.tabs.sendMessage(tab.id, {type: retry ? 'echo-youtube-retry' : 'echo-youtube-state'});
    output.textContent = !state.video ? '请打开 YouTube 视频。' :
      state.caption || '尚未读取到字幕，可开启视频的英文字幕后重试。';
    document.getElementById('yt-state').textContent = !state.enabled ? '已关闭' :
      {timeline: '时间轴预取', live: '实时字幕', ad: '广告中'}[state.source] || '等待字幕';
    // The ink bar already names the state; this line only adds detail or the error.
    document.getElementById('mode').textContent = !state.enabled ? '' : [
      state.source === 'timeline' ? `时间轴预取 · 提前 ${state.leadFrames} 帧（约 ${Math.round(state.leadFrames / state.fps * 1000)}ms）` :
        state.source === 'live' ? '无法提前读取，跟随画面字幕' : '', state.error].filter(Boolean).join(' · ');
  } catch (error) {
    output.textContent = `字幕伴读未启动：${error.message}`;
    document.getElementById('mode').textContent = '请在 YouTube 页面点击重试；如权限被限制，请允许扩展访问此网站。';
  }
}
document.getElementById('refresh').onclick = () => preview(true);
chrome.storage.local.get({enabled: true, leadFrames: 12}).then(settings => {
  const enabled = document.getElementById('enabled'), lead = document.getElementById('lead');
  enabled.checked = settings.enabled; lead.value = String(settings.leadFrames);
  enabled.disabled = lead.disabled = false;
  enabled.onchange = async () => { await chrome.storage.local.set({enabled: enabled.checked}); await preview(); };
  lead.onchange = async () => { await chrome.storage.local.set({leadFrames: Number(lead.value)}); await preview(); };
});
// Only a YouTube tab shows the caption section; other tabs never poll it.
let captionPreviewTimer;
captionTab().then(() => {
  void preview();
  // Keep the popup's waiting/source message in sync with asynchronous track loading.
  captionPreviewTimer = setInterval(() => { void preview(false, false); }, 1000);
}, () => {});
window.addEventListener('pagehide', () => clearInterval(captionPreviewTimer));
window.addEventListener('echo-connection-saved', () => { if (captionPreviewTimer) void preview(); });
