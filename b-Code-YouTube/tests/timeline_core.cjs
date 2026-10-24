const assert = require('node:assert/strict');
const core = require('../extension/timeline-core.js');
const cues = core.parse(JSON.stringify({events: [
  {tStartMs: 1000, dDurationMs: 3000, segs: [{utf8: 'The river '}, {utf8: 'bank.'}]},
  {tStartMs: 4000, dDurationMs: 2000, segs: [{utf8: 'Get a loan.'}]},
]}));
assert.deepEqual(cues, [{start: 1, end: 4, text: 'The river bank.'}, {start: 4, end: 6, text: 'Get a loan.'}]);
assert.equal(core.plan(cues, .59, .4).wanted.length, 0);
assert.deepEqual(core.plan(cues, .61, .4), {active: null, activeIndex: -1, wanted: [cues[0]]}, 'preload without early display');
assert.deepEqual(core.plan(cues, 3.8, .4), {active: cues[0], activeIndex: 0, wanted: cues}, 'active cue and next cue only');
assert.equal(core.plan(cues, 6.1, .4).active, cues[1], 'last subtitle remains after its end timestamp');
assert.equal(core.plan(cues, 600, .4).activeIndex, 1, 'final subtitle has no arbitrary display timeout');
assert.equal(core.plan(cues, 1.1, .4).active, cues[0], 'seeking backwards uses media timestamps');
const repeated = core.normalize([{start: 0, end: 1, text: 'Yes.'}, {start: 1, end: 2, text: 'Yes.'}]);
assert.equal(core.plan(repeated, 1.1, .4).activeIndex, 1, 'identical text retains its distinct cue occurrence');
const gaps = core.normalize([{start: 1, end: 2, text: 'First.'}, {start: 10, end: 11, text: 'Next.'}]);
assert.equal(core.plan(gaps, .5, .1).active, null, 'nothing is held before the first subtitle');
assert.deepEqual(core.plan(gaps, 9.7, .4), {active: gaps[0], activeIndex: 0, wanted: gaps}, 'gap keeps previous cue while prefetching the next');
assert.equal(core.plan(gaps, 10, .4).active, gaps[1], 'next subtitle replaces held cue at its start');
assert.equal(core.plan(gaps, 5, .4).active, gaps[0], 'seeking into a gap selects the preceding cue, never a future cue');
const rolling = core.parse(JSON.stringify({events: [
  {tStartMs: 0, dDurationMs: 4000, wWinId: 1, segs: [{utf8: 'Hello'}]},
  {tStartMs: 500, dDurationMs: 3000, wWinId: 1, aAppend: 1, segs: [{utf8: 'world'}]},
]}));
assert.equal(rolling[0].end, .5);
assert.equal(rolling[1].text, 'Hello world');
assert.deepEqual(core.parse('WEBVTT\n\n00:01.000 --> 00:03.000\n<i>Bank</i> &amp; river.\n'),
  [{start: 1, end: 3, text: 'Bank & river.'}]);
assert.equal(core.words('😀 bank')[0].start, 3, 'UTF-16 server offsets');
assert.equal(core.words("isn't quiet")[0].word, "isn't");
assert.deepEqual(core.normalize([{start: NaN, end: 2, text: 'bad'}, {start: 1, end: 2, text: 'x'.repeat(1601)}]), []);
console.log('PASS subtitle timestamps, advance scheduling, held gaps/final cue, seek, rolling ASR, VTT and UTF-16 words');
