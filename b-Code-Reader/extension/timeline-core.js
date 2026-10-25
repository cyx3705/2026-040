/* YT-002: media seconds, UTF-16 word offsets. No inference in this layer. */
(() => {
  const clean = text => String(text || '').replace(/[\u200b\ufeff]/g, '').replace(/\s+/g, ' ').trim();
  function normalize(input) {
    const cues = input.filter(cue => Number.isFinite(cue.start) && Number.isFinite(cue.end) &&
      cue.start >= 0 && cue.end > cue.start && cue.text && cue.text.length <= 1600)
      .map(cue => ({start: cue.start, end: cue.end, text: clean(cue.text)}))
      .filter(cue => cue.text).sort((a, b) => a.start - b.start).slice(0, 20000);
    for (let i = 0; i < cues.length - 1; i++) {
      if (cues[i + 1].start > cues[i].start) cues[i].end = Math.min(cues[i].end, cues[i + 1].start);
    }
    return cues;
  }
  function parse(body) {
    if (typeof body !== 'string' || body.length > 4 * 1024 * 1024) return [];
    const text = body.trim();
    if (text.startsWith('{')) {
      const data = JSON.parse(text), windows = new Map(), cues = [];
      for (const event of (data.events || []).slice(0, 40000)) {
        if (!Array.isArray(event.segs)) continue;
        const part = clean(event.segs.map(segment => segment.utf8 || '').join(''));
        const window = event.wWinId ?? 0;
        if (!part) { windows.delete(window); continue; }
        const full = event.aAppend ? clean(`${windows.get(window) || ''} ${part}`) : part;
        windows.set(window, full);
        const start = Number(event.tStartMs) / 1000;
        cues.push({start, end: start + Number(event.dDurationMs || 2000) / 1000, text: full});
      }
      return normalize(cues);
    }
    if (text.startsWith('<') && typeof DOMParser !== 'undefined') {
      const xml = new DOMParser().parseFromString(text, 'text/xml');
      if (xml.querySelector('parsererror')) return [];
      return normalize([...xml.querySelectorAll('text,p')].map(node => {
        const ms = node.localName === 'p';
        const start = Number(node.getAttribute(ms ? 't' : 'start')) / (ms ? 1000 : 1);
        return {start, end: start + Number(node.getAttribute(ms ? 'd' : 'dur')) / (ms ? 1000 : 1), text: node.textContent};
      }));
    }
    const seconds = value => value.split(':').reduce((n, part) => n * 60 + Number(part), 0), cues = [];
    for (const block of text.split(/\r?\n\s*\r?\n/)) {
      const lines = block.split(/\r?\n/), index = lines.findIndex(line => line.includes('-->'));
      if (index < 0) continue;
      const times = lines[index].match(/([\d:.]+)\s+-->\s+([\d:.]+)/);
      if (times) cues.push({start: seconds(times[1]), end: seconds(times[2]),
        text: lines.slice(index + 1).join(' ').replace(/<[^>]*>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')});
    }
    return normalize(cues);
  }
  function plan(cues, time, lead) {
    let lo = 0, hi = cues.length;
    while (lo < hi) { const mid = (lo + hi) >>> 1; if (cues[mid].start <= time) lo = mid + 1; else hi = mid; }
    // YT-006: hold the last started cue until the next cue starts. The media
    // layer stops displaying it when playback ends; cue.end is not a hide timer.
    const index = lo - 1, active = index >= 0 ? cues[index] : null;
    const upcoming = cues.slice(index + 1, index + 3).filter(cue => cue.start <= time + lead);
    return {active, activeIndex: active ? index : -1, wanted: [...(active ? [active] : []), ...upcoming].slice(0, 3)};
  }
  function words(text) {
    return [...text.matchAll(/[A-Za-z]+(?:['’-][A-Za-z]+)*/g)].map(match => ({
      start: match.index, end: match.index + match[0].length, word: match[0],
      lemma: match[0].toLowerCase(), gloss: '', state: 'pending',
    }));
  }
  const api = {clean, normalize, parse, plan, words};
  globalThis.EchoYouTubeTimeline = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
