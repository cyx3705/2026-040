/* Shared pure offset/chunk helpers, exercised directly by Node. */
(() => {
  const core = {
    chunks(text, limit = 600) {
      const out = [];
      let start = 0;
      while (start < text.length) {
        let end = Math.min(start + limit, text.length);
        if (end < text.length) {
          const boundary = text.lastIndexOf(' ', end);
          if (boundary > start + limit / 2) end = boundary + 1;
          // Never bisect a word or UTF-16 surrogate pair.
          while (end < text.length && /[A-Za-z'’\-]/.test(text[end - 1]) && /[A-Za-z'’\-]/.test(text[end])) end++;
          if (/^[\uDC00-\uDFFF]$/.test(text[end] || '')) end++;
        }
        out.push({start, end, text: text.slice(start, end)}); start = end;
      }
      return out;
    },
    ranges(parts, chunk, item) {
      const start = chunk.start + item.start, end = chunk.start + item.end;
      return parts.filter(p => p.start < end && p.end > start).map(p => ({
        node: p.node, start: Math.max(0, start - p.start), end: Math.min(p.end, end) - p.start,
      }));
    },
  };
  globalThis.EchoReaderCore = core;
  if (typeof module !== 'undefined') module.exports = core;
})();
