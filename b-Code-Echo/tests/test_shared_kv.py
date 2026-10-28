"""ECHO-KV-001: external leases, cancellation and strict result boundaries."""
import asyncio
import json
import unittest
import httpx
from echo_app.engine import Engine
from echo_app.kv import KVUnsupported, SequenceHandle


class SharedKVTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine()
        self.calls = []
        self.handler = None
        async def respond(request):
            self.assertEqual(request.url.path, '/v1/kv/op')
            body = json.loads(request.content)
            self.calls.append(body)
            if self.handler:
                response = await self.handler(body)
                if response is not None:
                    return response
            op = body['op']
            if op == 'capabilities':
                return httpx.Response(200, json={key:True for key in
                    ['supported','shared_prefix','fork_at','label_constraint','release']})
            if op == 'create':
                return httpx.Response(200, json={'sequence_id':'a1','cache_epoch':'e1','prefix_tokens':42})
            if op == 'tokenize':
                return httpx.Response(200, json={'tokens':[12,13]})
            if op == 'select':
                return httpx.Response(200, json={'results':[{'label':' B','decision_steps':1} for _ in body['jobs']]})
            if op == 'select_text':
                return httpx.Response(200, json={'results':[{'label':' B','decision_steps':1,
                    'shared_prefix_tokens':40,'replayed_tokens':2}]})
            return httpx.Response(200, json={'released':True})
        self.engine._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    async def asyncTearDown(self):
        await self.engine.aclose()

    async def test_capability_requires_all_real_primitives(self):
        self.assertTrue((await self.engine.kv_capabilities('m')).supported)
        async def partial(body):
            return httpx.Response(200, json={'supported':True,'shared_prefix':True,'fork_at':False})
        self.handler = partial
        self.assertFalse((await self.engine.kv_capabilities('m')).supported)

    async def test_whole_word_label_uses_one_decision_and_releases(self):
        label = await self.engine.shared_kv.choose('m', 'prompt', ['0',' A',' B'])
        self.assertEqual(label, 'B')
        self.assertEqual([c['op'] for c in self.calls], ['create','tokenize','select','release'])
        job = self.calls[2]['jobs'][0]
        self.assertEqual(job['prefix_tokens'],42)
        self.assertEqual(job['budget'],1)
        self.assertEqual(self.calls[-1]['cache_epoch'],'e1')
        self.assertEqual(self.engine._requests._value,1)

    async def test_invalid_label_is_rejected_and_lease_reclaimed(self):
        async def invalid(body):
            if body['op'] == 'select':
                return httpx.Response(200, json={'results':[{'label':' Z','decision_steps':1}]})
        self.handler = invalid
        with self.assertRaises(KVUnsupported):
            await self.engine.shared_kv.choose('m','prompt',[' A',' B'])
        self.assertEqual(self.calls[-1]['op'],'release')

    async def test_select_text_combines_prefix_tokenize_and_select(self):
        async with self.engine.shared_kv.sequence('m', 'prompt') as sequence:
            rows = await self.engine.shared_kv.select_text(sequence, 'prompt bank', 'Label:', ['0', ' B'])
        self.assertEqual(rows[0]['label'], ' B')
        self.assertEqual([c['op'] for c in self.calls], ['create', 'select_text', 'release'])
        self.assertEqual(self.engine._requests._value, 1)

    async def test_chat_prefetches_a_before_consumer_can_schedule_b(self):
        first_next = asyncio.Event()
        second_next = asyncio.Event()

        async def handler(body):
            if body['op'] == 'next':
                if not first_next.is_set():
                    first_next.set()
                    return httpx.Response(200, json={'done': False, 'delta': 'A'})
                second_next.set()
                return httpx.Response(200, json={'done': True, 'delta': ''})
            return httpx.Response(200, json={'released': True})

        self.handler = handler
        stream = self.engine.shared_kv.chat(SequenceHandle('a1', 'e1', 42), max_tokens=2)
        try:
            self.assertEqual(await stream.__anext__(), 'A')
            await asyncio.wait_for(second_next.wait(), timeout=1)
        finally:
            await stream.aclose()

    async def test_cancel_during_create_reclaims_late_handle(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(body):
            if body['op'] == 'create':
                entered.set()
                await finish.wait()
        self.handler = delayed
        task = asyncio.create_task(self.engine.shared_kv.choose('m','prompt',[' A',' B']))
        await entered.wait()
        task.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([c['op'] for c in self.calls],['create','release'])
        self.assertFalse(self.engine.shared_kv.lock.locked())
        self.assertEqual(self.engine._requests._value,1)

    async def test_stream_chat_uses_one_request_and_requires_completion_marker(self):
        async def streaming(body):
            if body['op'] == 'stream':
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                    content='data: {"delta":"Hello"}\n\ndata: {"delta":" world"}\n\n'
                            'data: {"done":true}\n\ndata: [DONE]\n\n')
        self.handler = streaming
        self.engine.shared_kv._stream_supported = True
        text = [delta async for delta in self.engine.shared_kv.chat(SequenceHandle('a1','e1',42), max_tokens=2)]
        self.assertEqual(''.join(text), 'Hello world')
        self.assertEqual([call['op'] for call in self.calls], ['stream'])
        self.assertEqual(self.calls[0]['max_tokens'], 2)

    async def test_stream_cancellation_closes_connection_and_releases_sequence(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"delta":"partial"}\n\n'
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        async def streaming(body):
            if body['op'] == 'stream':
                return httpx.Response(200, stream=SlowStream())
        self.handler = streaming
        self.engine.shared_kv._stream_supported = True
        async def consume():
            async with self.engine.shared_kv.sequence('m', 'prompt') as sequence:
                async for _ in self.engine.shared_kv.chat(sequence): pass
        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(closed.is_set())
        self.assertEqual(self.calls[-1]['op'], 'release')
        self.assertFalse(self.engine.shared_kv.lock.locked())
        self.assertEqual(self.engine._requests._value, 1)

    async def test_truncated_stream_fails_and_sequence_is_still_released(self):
        async def streaming(body):
            if body['op'] == 'stream':
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                    content='data: {"delta":"partial"}\n\n')
        self.handler = streaming
        self.engine.shared_kv._stream_supported = True
        with self.assertRaisesRegex(KVUnsupported, 'incomplete_shared_kv_stream'):
            async with self.engine.shared_kv.sequence('m', 'prompt') as sequence:
                async for _ in self.engine.shared_kv.chat(sequence): pass
        self.assertEqual([call['op'] for call in self.calls], ['create', 'stream', 'release'])

    async def test_cancelling_a_live_sequence_releases_and_restores_admission(self):
        entered = asyncio.Event()
        async def work():
            async with self.engine.shared_kv.sequence('m','prompt'):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.calls[-1]['op'],'release')
        self.assertFalse(self.engine.shared_kv.lock.locked())
