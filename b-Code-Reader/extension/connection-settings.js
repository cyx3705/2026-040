/* Popup-only settings; secrets never appear in a page message or URL. */
(() => {
  const $ = id => document.getElementById(id);
  async function display() {
    const value = await EchoConnection.settings();
    $('connection-url').value = value.url; $('connection-key').value = value.key;
    const local = ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(value.url).hostname);
    $('connection-open').hidden = !local; $('connection-open').href = value.url;
  }
  $('connection-save').onclick = async () => {
    $('connection-save').disabled = true;
    try {
      const value = EchoConnection.normalize({url: $('connection-url').value.trim(), key: $('connection-key').value});
      // Must request within the original click gesture, before other awaits.
      if (!await chrome.permissions.request({origins: [`${value.url}/*`]})) throw new Error('未授予主机访问权限，连接设置未改变');
      const saved = await chrome.runtime.sendMessage({type: 'echo-connection-save', connection: value});
      if (saved.error) throw new Error(saved.error);
      await display();
      try {
        const result = await chrome.runtime.sendMessage({type: 'echo-connection-status'});
        if (result.error) throw new Error(result.error);
        $('connection-message').textContent = `已保存并连接 · ${result.data.model || ''}`;
        window.dispatchEvent(new Event('echo-connection-saved'));
      } catch (error) {
        $('connection-message').textContent = `已保存，连接失败：${error.message}。请检查主机服务、地址及防火墙。`;
        window.dispatchEvent(new Event('echo-connection-saved'));
      }
    } catch (error) { $('connection-message').textContent = error.message; }
    finally { $('connection-save').disabled = false; }
  };
  void display().catch(error => { $('connection-message').textContent = error.message; });
})();
