/* NET-001: configured host only, fixed API paths, no page-controlled proxy. */
(() => {
  const defaults = {url: 'http://127.0.0.1:8040', key: ''};
  const routes = new Set(['/api/status', '/api/reader/learned', '/api/reader/gloss', '/api/learned']);
  function normalize(value = defaults) {
    const url = new URL(value.url || defaults.url), key = String(value.key || '').trim();
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
      throw new Error('请填写 HTTP/HTTPS 主机地址，例如 http://192.168.1.20:8040，不含路径、账号或参数');
    }
    const local = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((!local || key) && !/^[\x21-\x7e]{32,256}$/.test(key)) throw new Error('远程连接需要主机提供的 32～256 位访问密钥');
    return {url: url.origin, key};
  }
  async function settings() {
    return normalize((await chrome.storage.local.get('connection')).connection || defaults);
  }
  async function api(path, body, signal = AbortSignal.timeout(10000)) {
    if (!routes.has(path)) throw new Error('不支持的 Echo 接口');
    const value = await settings();
    const response = await fetch(value.url + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {...(body === undefined ? {} : {'Content-Type': 'application/json'}),
        ...(value.key ? {Authorization: `Bearer ${value.key}`} : {})},
      body: body === undefined ? undefined : JSON.stringify(body), signal,
      credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', cache: 'no-store',
    });
    if (response.status === 401) throw new Error('访问密钥不正确，或主机尚未启用共享模式');
    if (!response.ok) throw new Error(`Echo 接口 ${response.status}`);
    return response;
  }
  globalThis.EchoConnection = {normalize, settings, api};
})();
