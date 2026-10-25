/* YT-002: MAIN world only; page-session English tracks, no loopback APIs. */
(() => {
  if (window.__echoYouTubeBridge) return;
  window.__echoYouTubeBridge = true;
  const nativeFetch = window.fetch;
  let enabled = false, controller, trackKey = '', lastAttempt = 0;
  // YT-010: SPA navigation can fetch the new video's track before the isolated
  // world sees the new URL. Keep recent tracks by their own v= so a late request
  // still gets them; the native player won't fetch the same track again.
  const tracks = new Map();
  const videoId = () => location.pathname === '/watch' ? new URL(location.href).searchParams.get('v') :
    location.pathname.match(/^\/shorts\/([^/]+)/)?.[1];
  const emit = (id, data) => window.postMessage({source: 'echo-youtube-page', videoId: id, ...data}, location.origin);
  function trackURL(value) {
    try {
      const url = new URL(value, location.origin);
      return url.origin === 'https://www.youtube.com' && url.pathname === '/api/timedtext' && url.searchParams.get('v') &&
        /^en(?:-|$)/i.test(url.searchParams.get('lang') || '') && !url.searchParams.has('tlang') ? url : null;
    } catch { return null; }
  }
  function usable(body) {
    if (typeof body !== 'string' || !body.trim() || body.length > 4 * 1024 * 1024) return false;
    try {
      if (body.trim().startsWith('{')) return Boolean(JSON.parse(body).events?.length);
      return /(?:<transcript|<timedtext|^WEBVTT)/.test(body.trim());
    } catch { return false; }
  }
  function send(id) {
    const track = tracks.get(id);
    if (enabled && track && id === videoId()) emit(id, {kind: 'timeline', track: track.key, language: 'en', body: track.body});
  }
  function publish(body, key, id) {
    if (!id || !usable(body)) return false;
    const same = tracks.get(id)?.body === body;
    tracks.delete(id); tracks.set(id, {key: `${id}:${key}`, body});
    while (tracks.size > 8) tracks.delete(tracks.keys().next().value);
    if (!same) send(id);
    return true;
  }
  // Native requests may have fresh playback tokens absent in player metadata.
  // Observe response copies; the website still receives its original response.
  window.fetch = function (...args) {
    const result = Reflect.apply(nativeFetch, this, args), url = trackURL(args[0]?.url || args[0]);
    if (url) {
      const id = url.searchParams.get('v');
      result.then(response => {
        if (response.ok) void response.clone().text().then(body => publish(body, url.searchParams.get('lang'), id)).catch(() => {});
      }).catch(() => {});
    }
    return result;
  };
  const nativeOpen = XMLHttpRequest.prototype.open, nativeSend = XMLHttpRequest.prototype.send, requests = new WeakMap();
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    requests.set(this, trackURL(url));
    return Reflect.apply(nativeOpen, this, [method, url, ...rest]);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    const url = requests.get(this), id = url?.searchParams.get('v');
    if (url) this.addEventListener('load', () => {
      if (this.status !== 200) return;
      try { publish(this.responseType === 'json' ? JSON.stringify(this.response) : this.responseText,
        url.searchParams.get('lang'), id); } catch { /* Binary data is not a caption track. */ }
    }, {once: true});
    return Reflect.apply(nativeSend, this, args);
  };
  async function request() {
    const id = videoId();
    if (!enabled || !id) return;
    if (tracks.has(id)) { send(id); return; }
    const player = document.querySelector('#movie_player, .html5-video-player');
    let response, selected;
    try { response = player?.getPlayerResponse?.() || window.ytInitialPlayerResponse;
      selected = player?.getOption?.('captions', 'track'); } catch { return; }
    // Right after navigation the player can still describe the previous video.
    if (response?.videoDetails?.videoId !== id) return;
    const list = response.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
    const english = list.filter(track => /^en(?:-|$)/i.test(track.languageCode || ''));
    const track = english.find(track => track.vssId === selected?.vssId) || english.find(track => track.kind !== 'asr') || english[0];
    const url = track && trackURL(track.baseUrl);
    if (!url || url.searchParams.get('v') !== id) { emit(id, {kind: 'unavailable', reason: '未找到可提前读取的英文字幕轨道'}); return; }
    const key = `${id}:${track.vssId || track.languageCode}`;
    if (key === trackKey && Date.now() - lastAttempt < 30000) return;
    controller?.abort(); controller = new AbortController();
    const active = controller;
    trackKey = key; lastAttempt = Date.now();
    url.searchParams.set('fmt', 'json3');
    const timeout = setTimeout(() => active.abort(), 8000);
    try {
      const result = await Reflect.apply(nativeFetch, window, [url.href, {credentials: 'include', signal: active.signal}]);
      if (!result.ok) throw new Error(`字幕接口 ${result.status}`);
      const body = await result.text();
      if (!body.trim()) throw new Error('字幕接口返回空内容，可开启视频英文字幕');
      if (!publish(body, track.vssId || track.languageCode, id)) throw new Error('未返回可用的字幕时间轴');
    } catch (error) {
      if (controller === active && enabled && videoId() === id && !tracks.has(id)) emit(id, {kind: 'unavailable', reason: error.message});
    } finally { clearTimeout(timeout); }
  }
  window.addEventListener('message', event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.source !== 'echo-youtube-extension') return;
    if (event.data.kind === 'stop') { enabled = false; controller?.abort(); trackKey = ''; return; }
    if (event.data.kind === 'request-timeline') { enabled = true; void request(); }
  });
})();
