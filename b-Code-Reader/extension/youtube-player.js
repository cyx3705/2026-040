/* YT-012: shared by MAIN and isolated worlds; Shorts keeps retired players in
   the document, so selection must stay within the active reel when available. */
(() => {
  const selector = '#movie_player, #shorts-player, .html5-video-player';
  function select(id) {
    if (!id) return null;
    const shorts = location.pathname.startsWith('/shorts/');
    const reel = shorts && document.querySelector('ytd-reel-video-renderer[is-active], ytd-reel-video-renderer[active]');
    const root = reel || document;
    const players = [...root.querySelectorAll(selector)];
    // Only MAIN can call player methods. A recycled inactive player may still
    // report the requested ID; never search outside an explicitly active reel.
    const match = players.find(player => {
      try { return player.getPlayerResponse?.()?.videoDetails?.videoId === id; } catch { return false; }
    });
    if (match) return match;
    if (reel) return players.find(player => player.querySelector('video')) || players[0] || null;
    return document.querySelector(shorts ? '#shorts-player' : '#movie_player') ||
      players.find(player => player.querySelector('video') && player.getClientRects().length) || players[0] || null;
  }
  globalThis.EchoYouTubePlayer = {select};
})();
