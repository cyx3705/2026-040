"""EXT-001: external transport, no provider lifecycle changes or live requests."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from echo_app import config
from echo_app.engine import Engine


class ExternalEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine()
        self.requests = []

    async def asyncTearDown(self):
        await self.engine.aclose()

    def connect(self, handler):
        def respond(request):
            self.requests.append(request)
            return handler(request)
        self.engine._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    async def test_chat_uses_structured_messages_and_skips_reasoning(self):
        def respond(request):
            body = json.loads(request.content)
            self.assertEqual(body['messages'][0], {'role': 'system', 'content': 'Speak English.'})
            self.assertNotIn('prompt', body)
            return httpx.Response(200, text=': keepalive\n\ndata: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\ndata: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n')
        self.connect(respond)
        self.assertEqual([s async for s in self.engine.chat('m', 'Speak English.', [{'role':'user','content':'Hi'}])], ['Hello'])
        self.assertEqual(self.requests[0].url.path, '/v1/chat/completions')

    async def test_truncated_and_error_streams_fail_instead_of_success(self):
        for data in ['data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', 'data: {"error":{"message":"failed"}}\n\n']:
            await self.engine.aclose()
            self.connect(lambda _: httpx.Response(200, text=data))
            with self.assertRaises(RuntimeError):
                _ = [s async for s in self.engine.chat('m', '', [{'role':'user','content':'Hi'}])]

    async def test_schema_and_model_pass_through_and_length_is_rejected(self):
        schema = {'type':'object','properties':{'label':{'enum':['0','A']}},'required':['label']}
        self.connect(lambda _: httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'{"label":"A"}'}}]}))
        self.assertEqual(await self.engine.ask('external-model', 'task', format_schema=schema), '{"label":"A"}')
        body = json.loads(self.requests[0].content)
        self.assertEqual(body['response_format']['json_schema']['schema'], schema)
        self.assertEqual(body['model'], 'external-model')
        self.assertNotIn('keep_alive', body)
        await self.engine.aclose()
        self.connect(lambda _: httpx.Response(200, json={'choices':[{'finish_reason':'length','message':{'content':'{}'}}]}))
        with self.assertRaises(RuntimeError):
            await self.engine.ask('m', 'task')

    async def test_next_token_exact_cache_is_model_scoped(self):
        self.connect(lambda _: httpx.Response(200, json={'completion_probabilities':[{'top_logprobs':[{'token':' English','logprob':-1.0}]}]}))
        for model in ['a', 'a', 'b']:
            self.assertEqual((await self.engine.kv_top_tokens('I learn', model=model))[0]['token'], ' English')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(json.loads(self.requests[0].content)['n_predict'], 1)
        self.assertEqual(self.requests[0].url.path, '/completion')
        with patch.object(config, 'COMPLETION_URL', ''):
            with self.assertRaisesRegex(RuntimeError, '未配置'):
                await self.engine.kv_top_tokens('new')

    async def test_parallel_limit_is_local_and_no_lifecycle_api_exists(self):
        active = peak = 0
        async def respond(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}]})
        self.engine._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        for parallel in [1, 2]:
            peak = 0
            await self.engine.set_parallel(parallel, 'm')
            await asyncio.gather(*(self.engine.ask('m', 'task') for _ in range(4)))
            self.assertEqual(peak, parallel)
        for method in ['start','stop','load','unload','start_kv','_stop_kv']:
            self.assertFalse(hasattr(self.engine, method), method)
        self.assertFalse((await self.engine.kv_capabilities('m')).supported)

    async def test_provider_switch_polls_job_and_reports_rollback(self):
        state = {'state':'ready','current':'new','managed':True,'job_id':'j1'}
        disconnect_once = [True]
        def respond(request):
            if request.method == 'POST':
                self.assertEqual(json.loads(request.content),{'name':'new'})
                return httpx.Response(202,json={'state':'switching','job_id':'j1'})
            if disconnect_once:
                disconnect_once.pop()
                raise httpx.ConnectError('proxy restarting',request=request)
            return httpx.Response(200,json=state)
        self.engine._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch('asyncio.sleep',new_callable=AsyncMock):
            await self.engine.select_external_model('new')
            state.update(state='failed',current='old',error='原模型已恢复')
            with self.assertRaisesRegex(RuntimeError,'原模型已恢复'):
                await self.engine.select_external_model('new')

    async def test_cancel_stream_closes_upstream_and_releases_request_slot(self):
        closed = asyncio.Event()
        waiting = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                waiting.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        self.connect(lambda _: httpx.Response(200, stream=Stream()))
        async def consume():
            async for _ in self.engine.chat('m', '', [{'role':'user','content':'Hi'}]):
                pass
        task = asyncio.create_task(consume())
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertEqual(self.engine._requests._value, 1)


class ExternalInventoryTests(unittest.TestCase):
    def test_inventory_only_queries_models_and_supports_qwen35(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'data':[{'id':'qwen3.5:35b-a3b-q4_K_M'}]})
        original = httpx.Client
        with patch('echo_app.engine.httpx.Client', side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw)):
            engine = Engine()
            self.assertEqual(engine.models()[0]['family'], 'qwen')
            self.assertEqual(engine.status('qwen3.5:35b-a3b-q4_K_M')['state'], 'ready')
        self.assertTrue(all(r.method == 'GET' and r.url.path == '/v1/models' for r in requests))
