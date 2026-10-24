const assert = require('node:assert/strict');
const core = require('../browser-extension/reader-core.js');
const text = '😀 The bank is beside the river. '.repeat(100);
const chunks = core.chunks(text);
assert.equal(chunks.map(c => c.text).join(''), text);
assert(chunks.length > 1);
for (const c of chunks) {
  assert(c.text.length <= 650);
  assert(!/^[\uDC00-\uDFFF]/.test(c.text));
  assert(!/[\uD800-\uDBFF]$/.test(c.text));
}
const parts = [{node: 'a', start: 0, end: 7}, {node: 'b', start: 7, end: 12}];
assert.deepEqual(core.ranges(parts, {start: 0}, {start: 5, end: 9}), [
  {node: 'a', start: 5, end: 7}, {node: 'b', start: 0, end: 2},
]);
assert.equal(core.chunks('x'.repeat(1700))[0].text.length, 1700); // Never split a lexical item.
console.log('PASS reader UTF-16 chunks, inline ranges and intact text');
