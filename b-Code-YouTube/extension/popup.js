async function preview(retry = false) {
  const output = document.getElementById('caption');
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const state = await chrome.tabs.sendMessage(tab.id, {type: retry ? 'echo-youtube-retry' : 'echo-youtube-state'});
    output.textContent = !state.video ? '请打开 YouTube 视频。' :
      state.caption || '尚未读取到字幕，可开启视频的英文字幕后重试。';
    document.getElementById('mode').textContent = !state.enabled ? '字幕伴读已关闭' :
      `${state.source === 'timeline' ? `时间轴预取 · 提前 ${state.leadFrames} 帧（约 ${Math.round(state.leadFrames / state.fps * 1000)}ms 媒体时间）` :
        state.source === 'live' ? '实时字幕 · 当前无法提前读取' : state.source === 'ad' ? '广告播放中，字幕暂停' : '正在等待字幕'}${state.error ? ` · ${state.error}` : ''}`;
  } catch {
    output.textContent = '请打开或刷新 YouTube 视频页。';
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
void preview();
chrome.runtime.sendMessage({type: 'echo-youtube-status'}).then(result => {
  document.getElementById('service').textContent = result.error ?
    '本机 Echo 未连接，请先运行 echo-serve。' : `本机 Echo 已连接 · ${result.data.model || ''}`;
}).catch(() => {
  document.getElementById('service').textContent = '本机 Echo 未连接，请先运行 echo-serve。';
});
