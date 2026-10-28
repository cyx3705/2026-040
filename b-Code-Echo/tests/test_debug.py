"""EXT-001～004: external-service API, settings and learning boundaries."""

import asyncio
import hashlib
import importlib
import json
import re
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import httpx

from echo_app import config
from echo_app.engine import Engine
from echo_app.kv import (
    BranchHandle,
    KVCapabilities,
    KVForkCoordinator,
    KVUnsupported,
    SequenceHandle,
)


class KVForkContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_selection_releases_branch(self):
        entered = asyncio.Event()
        backend = Mock()
        backend.fork_at = AsyncMock(return_value=BranchHandle("b1", "a1", "e1"))

        async def wait(*args):
            entered.set()
            await asyncio.Event().wait()

        backend.decode_label = wait
        backend.release = AsyncMock()
        coordinator = KVForkCoordinator(backend)
        task = asyncio.create_task(coordinator.select_label(SequenceHandle("a1", "e1", 12), 10, [1], ["0", "A"]))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        backend.release.assert_awaited_once()
        self.assertFalse(coordinator.active)

    async def test_coordinator_releases_branch_after_success(self):
        backend = Mock()
        backend.fork_at = AsyncMock(return_value=BranchHandle("b1", "a1", "e1"))
        backend.decode_label = AsyncMock(return_value="B")
        backend.release = AsyncMock()
        coordinator = KVForkCoordinator(backend)
        result = await coordinator.select_label(
            SequenceHandle("a1", "e1", 12), 10, [1, 2], ["0", "A", "B"]
        )
        self.assertEqual(result, "B")
        self.assertFalse(coordinator.active)
        backend.release.assert_awaited_once()

    async def test_coordinator_releases_branch_after_decode_failure(self):
        backend = Mock()
        branch = BranchHandle("b1", "a1", "e1")
        backend.fork_at = AsyncMock(return_value=branch)
        backend.decode_label = AsyncMock(side_effect=RuntimeError("decode"))
        backend.release = AsyncMock()
        coordinator = KVForkCoordinator(backend)
        with self.assertRaisesRegex(RuntimeError, "decode"):
            await coordinator.select_label(SequenceHandle("a1", "e1", 12), 10, [], ["0", "A"])
        self.assertFalse(coordinator.active)
        backend.release.assert_awaited_once_with(branch)

    async def test_unconfigured_remote_backend_reports_unsupported(self):
        from echo_app.kv import RemoteKVBackend

        caps = await RemoteKVBackend(None).capabilities("qwen3:8b")
        self.assertEqual(caps, KVCapabilities.unsupported("ollama-http", "adapter_not_configured"))
        with self.assertRaisesRegex(KVUnsupported, "adapter_not_configured"):
            await RemoteKVBackend(None).fork_at(SequenceHandle("a", "e", 1), 1)


class DebugApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # server 导入时会建立用户库，测试必须隔离到临时目录。
        cls.temp = tempfile.TemporaryDirectory()
        with patch.object(config, "USER_DB", Path(cls.temp.name) / "user.db"), \
                patch.object(config, "KV_ADAPTER_URL", None):
            cls.server = importlib.import_module("echo_app.server")

    @classmethod
    def tearDownClass(cls):
        cls.server.store._con.close()
        cls.server.dictionary._user.close()
        cls.temp.cleanup()

    async def asyncSetUp(self):
        # These are generic external-service tests. Native capability discovery
        # and handle ownership have their own hermetic transport tests.
        probe = patch.object(self.server, "shared_available", new=AsyncMock(return_value=None))
        probe.start()
        self.addCleanup(probe.stop)
        management = patch.object(self.server.engine, 'model_switch', new=AsyncMock(return_value=None))
        management.start()
        self.addCleanup(management.stop)
        catalog = patch.object(self.server.engine, 'model_catalog', side_effect=lambda: {
            'managed':False,'models':[{**m,'installed':True,'supported':True} for m in self.server.engine.models()]})
        catalog.start()
        self.addCleanup(catalog.stop)
        self.server.engine.parallel = 1
        self.server.store.set_setting("suggest_candidates", "5")
        self.server.store.set_setting("engine_parallel", "1")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://127.0.0.1")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_shared_extensions_use_the_same_real_temporary_learning_database(self):
        key = "fixture-key-" + "x" * 32
        headers = {"Authorization": f"Bearer {key}"}
        with patch.object(self.server.app.state, "share_key", key, create=True), \
                patch.object(self.server.dictionary, "lookup") as lookup:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app,
                    client=("192.168.1.2", 1200)), base_url="http://192.168.1.10", headers=headers) as reader, \
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app,
                    client=("192.168.1.3", 1201)), base_url="http://192.168.1.10", headers=headers) as youtube:
                result = await reader.post("/api/learned", json={"lemma": "bank", "learned": True})
                self.assertEqual(result.status_code, 200)
                self.assertIn({"lemma": "bank"}, (await youtube.get("/api/reader/learned")).json())
                self.assertIn({"lemma": "bank"}, (await self.client.get("/api/reader/learned")).json())
                await youtube.post("/api/learned", json={"lemma": "bank", "learned": False})
                self.assertNotIn({"lemma": "bank"}, (await reader.get("/api/reader/learned")).json())
                self.assertNotIn("bank", self.server.dictionary.learned_lemmas())
                lookup.assert_not_called()

    def test_share_command_binds_network_without_trusting_forwarded_headers(self):
        with patch("sys.argv", ["echo-serve", "--share", "--no-browser"]), \
                patch.object(config, "WEB_HOST", "127.0.0.1"), \
                patch.object(config, "SHARE_KEY", "fixture-key-" + "x" * 32), \
                patch.object(self.server.app.state, "share_key", None, create=True), \
                patch.object(self.server, "already_running", return_value=False), \
                patch.object(self.server.uvicorn, "run") as run, \
                patch.object(self.server.webbrowser, "open") as browser:
            self.server.main()
            self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
            self.assertFalse(run.call_args.kwargs["proxy_headers"])
            browser.assert_not_called()

    async def test_reader_all_learned_stream_skips_chinese_and_native_calls(self):
        dictionary = Mock()
        dictionary.learned_lemmas.return_value = {"bank"}
        dictionary.lemma.return_value = "bank"
        with patch.object(self.server, "dictionary", dictionary), \
                patch.object(self.server, "model_family", return_value="qwen3"), \
                patch.object(self.server.engine, "ask", new_callable=AsyncMock) as create:
            response = await self.client.post("/api/reader/gloss", json={"text": "bank"})
            self.assertEqual(response.status_code, 200)
            self.assertIn('"state": "skipped_learned"', response.text)
            self.assertIn('"input_tokens": 0', response.text)
            learned = await self.client.get("/api/reader/learned")
            self.assertEqual(learned.json(), [{"lemma": "bank"}])
            dictionary.lookup.assert_not_called()
            dictionary.learned.assert_not_called()
            create.assert_not_awaited()

    async def test_reader_rejects_empty_and_oversized_text(self):
        for text in ("", "x" * 1601):
            response = await self.client.post("/api/reader/gloss", json={"text": text})
            self.assertEqual(response.status_code, 422)

    async def test_page_links_current_assets_and_cached_assets_are_revalidated(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-cache")
        urls = re.findall(r'"(/static/(?:app\.(?:css|js)|composer_input\.js)\?v=[a-f0-9]+)"', response.text)
        self.assertEqual(len(urls), 3)
        for url in urls:
            asset = await self.client.get(url)
            self.assertEqual(asset.status_code, 200)
            self.assertEqual(url.split("?v=")[1], hashlib.sha256(asset.content).hexdigest()[:16])
            cached = await self.client.get(url, headers={"If-None-Match": asset.headers["etag"]})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers["cache-control"], "no-cache")
        # 兼容旧页面仍使用无版本地址的刷新。
        legacy = await self.client.get("/static/app.css")
        self.assertEqual(legacy.headers["cache-control"], "no-cache")

    async def test_status_reports_exact_kv_capabilities(self):
        with patch.object(self.server.engine, "status", return_value={"state": "ready"}), \
                patch.object(
                    self.server.engine, "kv_capabilities", new_callable=AsyncMock,
                    return_value=KVCapabilities.unsupported("ollama-http", "adapter_not_configured"),
                ):
            response = await self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kv"], {
            "backend": "ollama-http",
            "supported": False,
            "shared_prefix": False,
            "fork_at": False,
            "label_constraint": False,
            "release": False,
            "reason": "adapter_not_configured",
        })

    async def test_input_mode_rejects_foreign_and_missing_origin(self):
        with patch.object(self.server.input_bridge, "acquire") as acquire:
            for headers, expected in (({}, 403), ({"origin": "http://other-site"}, 401)):
                response = await self.client.post("http://127.0.0.1:8040/api/input-mode", headers=headers,
                                                  json={"action": "acquire", "marker": "a" * 32})
                self.assertEqual(response.status_code, expected)
            rebound = await self.client.post("http://echo/api/input-mode", headers={"origin": "http://echo"},
                                             json={"action": "acquire", "marker": "a" * 32})
            self.assertEqual(rebound.status_code, 401)
            acquire.assert_not_called()

    async def test_input_mode_focus_challenge_and_owner_are_forwarded(self):
        state = {"owner": 71, "active": True, "ready": False, "chinese": False, "failed": False}
        with patch.object(self.server.input_bridge, "acquire", return_value=state) as acquire, \
                patch.object(self.server.input_bridge, "confirm", return_value={**state, "ready": True}) as confirm, \
                patch.object(self.server.input_bridge, "bounds") as bounds, \
                patch.object(self.server.input_bridge, "release") as release:
            headers = {"origin": "http://127.0.0.1:8040"}
            url = "http://127.0.0.1:8040/api/input-mode"
            response = await self.client.post(url, headers=headers,
                                              json={"action": "acquire", "marker": "a" * 32})
            self.assertFalse(response.json()["ready"])
            anchor = {"left": 20, "top": 400, "width": 300, "height": 26,
                      "viewport_width": 1280, "viewport_height": 720, "font_size": 17,
                      "color": 0, "background": 0xffffff}
            response = await self.client.post(url, headers=headers,
                                              json={"action": "confirm", "owner": 71, "bounds": anchor})
            self.assertTrue(response.json()["ready"])
            bounds.assert_called_once_with(71, anchor)
            invalid = await self.client.post(url, headers=headers,
                json={"action": "refresh", "owner": 71, "bounds": {**anchor, "width": 0}})
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(bounds.call_count, 1, "invalid geometry must not reach the native helper")
            response = await self.client.post(url, headers=headers, json={"action": "release", "owner": 71})
            self.assertEqual(response.status_code, 200)
            acquire.assert_called_once_with("a" * 32); confirm.assert_called_once_with(71); release.assert_called_once_with(71)

    async def test_input_mode_failure_is_explicit_and_does_not_claim_readiness(self):
        with patch.object(self.server.input_bridge, "acquire", side_effect=RuntimeError("input_focus_unavailable")):
            response = await self.client.post("http://127.0.0.1:8040/api/input-mode", headers={"origin": "http://127.0.0.1:8040"},
                                              json={"action": "acquire", "marker": "a" * 32})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "input_focus_unavailable")

    async def test_gloss_selector_returns_normal_and_degraded_occurrence_states(self):
        with patch.object(self.server.dictionary, "learned_lemmas", return_value=set()), \
                patch.object(self.server.engine, "ask", new_callable=AsyncMock) as ask:
            ask.return_value = '{"w2":"B"}'
            normal = await self.client.post(
                "/api/gloss", json={"text": "It looks like rain", "context": []}
            )
            ask.return_value = '{"w0":"0"}'
            degraded = await self.client.post("/api/gloss", json={"text": "like", "context": []})
        self.assertEqual(normal.status_code, 200)
        self.assertEqual(degraded.status_code, 200)
        normal_like = next(item for item in normal.json() if item["word"] == "like")
        self.assertEqual(normal_like["state"], "normal")
        self.assertEqual(normal_like["source"], "model")
        self.assertIsNone(normal_like["fallback_reason"])
        self.assertEqual(degraded.json()[0]["state"], "degraded")
        self.assertEqual(degraded.json()[0]["source"], "default")
        self.assertEqual(degraded.json()[0]["fallback_reason"], "unresolved")
        selector_prompt = ask.call_args_list[0].args[1]
        self.assertNotIn("rain", selector_prompt)

    async def test_gloss_selector_skips_learned_words_without_model_call(self):
        with patch.object(self.server.dictionary, "learned_lemmas", return_value={"like"}), \
                patch.object(self.server.dictionary, "lookup") as lookup, \
                patch.object(self.server.dictionary, "senses") as senses, \
                patch.object(self.server.engine, "ask", new_callable=AsyncMock) as ask:
            response = await self.client.post("/api/gloss", json={"text": "like", "context": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["state"], "skipped_learned")
        self.assertEqual(response.json()[0]["source"], "learned")
        ask.assert_not_awaited()
        lookup.assert_not_called()
        senses.assert_not_called()

    async def test_history_default_never_requests_model(self):
        with patch.object(self.server.engine, "ask", new_callable=AsyncMock) as ask:
            response = await self.client.post("/api/gloss", json={"text": "bank", "default_only": True})
        self.assertEqual(response.json()[0]["fallback_reason"], "legacy_default")
        ask.assert_not_awaited()

    async def test_css_update_changes_asset_url_without_editing_html(self):
        with tempfile.TemporaryDirectory() as folder:
            static = Path(folder)
            for name in ("index.html", "app.css", "app.js", "composer_input.js"):
                (static / name).write_bytes((self.server.STATIC / name).read_bytes())
            with patch.object(self.server, "STATIC", static):
                before = await self.client.get("/")
                with (static / "app.css").open("ab") as css:
                    css.write(b"\n/* updated style */\n")
                after = await self.client.get("/")
            pattern = r'"(/static/app.css\?v=[a-f0-9]+)"'
            self.assertNotEqual(re.search(pattern, before.text)[1], re.search(pattern, after.text)[1])

    async def test_saves_selected_count_and_returns_applied_parallel(self):
        async def apply(parallel, model):
            self.server.engine.parallel = parallel

        with patch.object(self.server.engine, "set_parallel", side_effect=apply) as change:
            response = await self.client.post("/api/debug", json={"candidates": 12, "parallel": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"candidates": 12, "parallel": 2, "max_candidates": 20})
        self.assertEqual(self.server.store.setting("engine_parallel", ""), "2")
        self.assertEqual(self.server.store.setting("suggest_candidates", ""), "12")
        change.assert_awaited_once_with(2, self.server.current_model())

    async def test_rejects_invalid_counts_and_five_way_parallel(self):
        with patch.object(self.server.engine, "set_parallel", new_callable=AsyncMock) as change:
            for candidates, parallel in [(0, 1), (21, 1), (5, 5), (5, 0), (2.5, 1)]:
                response = await self.client.post("/api/debug", json={"candidates": candidates, "parallel": parallel})
                self.assertEqual(response.status_code, 422)
            response = await self.client.post("/api/suggest", json={"messages": [], "text": "", "candidates": 21})
            self.assertEqual(response.status_code, 422)
            change.assert_not_awaited()

    async def test_input_translation_structured_selection_failure_and_abstention(self):
        with patch.object(self.server.dictionary, "english_choices", return_value=("learn", "study")), \
                patch.object(self.server.engine, "ask", new=AsyncMock(side_effect=[
                    '{"label":"B"}', '{"label":"0"}', RuntimeError("fail")])) as ask:
            payload = {"messages": [], "chinese": "学习", "before": "I ", "after": " English"}
            first = await self.client.post("/api/input-translation", json=payload)
            self.assertEqual(first.json(), {"word": "study", "reason": None})
            self.assertIn('"before": "I "', ask.call_args.args[1])
            self.assertEqual(ask.call_args.kwargs["format_schema"]["properties"]["label"]["enum"], ["0", "A", "B"])
            self.assertEqual((await self.client.post("/api/input-translation", json=payload)).json()["reason"], "unresolved")
            self.assertEqual((await self.client.post("/api/input-translation", json=payload)).json()["reason"], "selector_failure")

    async def test_input_translation_missing_choices_never_runs_model(self):
        with patch.object(self.server.dictionary, "english_choices", return_value=()), \
                patch.object(self.server.engine, "ask", new_callable=AsyncMock) as ask:
            for term, reason in [("不存在的词", "no_dictionary_choices"), ("hello", "chinese_word_required")]:
                response = await self.client.post("/api/input-translation", json={"messages": [], "chinese": term})
                self.assertEqual(response.json(), {"word": None, "reason": reason})
            ask.assert_not_awaited()

    async def test_shared_chat_sense_uses_only_actual_left_prefix(self):
        shared = Mock()
        shared.select_text = AsyncMock(return_value=[{'label':' A','decision_steps':1,
            'shared_prefix_tokens':10,'replayed_tokens':2}])
        sequence = SequenceHandle('a','epoch',20)
        with patch.object(self.server.dictionary,'learned_lemmas',return_value=set()), \
                patch.object(self.server.dictionary,'lemma',side_effect=str.lower), \
                patch.object(self.server.dictionary,'lookup',return_value={'lemma':'bank','gloss':'银行'}), \
                patch.object(self.server.dictionary,'senses',return_value=[{'pos':'n','text':'银行'},{'pos':'n','text':'岸'}]):
            result=await self.server.resolve_glosses('bank FUTURE_SECRET', [], shared=shared,
                sequence=sequence, source_prompt='PROMPT:',template='chatml')
        self.assertEqual(shared.select_text.call_args_list[0].args[:2], (sequence,'PROMPT:bank'))
        self.assertNotIn('FUTURE_SECRET', shared.select_text.call_args_list[0].args[2])
        self.assertEqual(result[0]['decision_steps'],1)
        self.assertEqual(result[0]['shared_prefix_tokens'],10)

    async def test_shared_chat_glosses_each_completed_word_before_next_stream_event(self):
        class FakeShared:
            def __init__(self):
                self.selected = 0

            @asynccontextmanager
            async def sequence(self, model, prompt):
                yield SequenceHandle('a', 'epoch', 12)

            async def chat(self, sequence):
                yield 'bank '

            async def prefix(self, sequence, text):
                return 16, ''

            async def tokenize(self, text):
                return [7, 8]

            async def select(self, sequence, jobs):
                self.selected += len(jobs)
                return [{'label': ' A', 'decision_steps': 1,
                         'shared_prefix_tokens': 14, 'replayed_tokens': 2}]

        shared = FakeShared()
        with patch.object(self.server, 'shared_available', new=AsyncMock(return_value=shared)), \
                patch.object(self.server, 'model_family', return_value='qwen'), \
                patch.object(self.server.dictionary, 'learned_lemmas', return_value=set()), \
                patch.object(self.server.dictionary, 'lemma', side_effect=str.lower), \
                patch.object(self.server.dictionary, 'lookup', return_value={'lemma':'bank','gloss':'银行'}), \
                patch.object(self.server.dictionary, 'senses', return_value=[
                    {'pos':'n','text':'银行'}, {'pos':'n','text':'岸'}]):
            response = await self.client.post('/api/chat', json={
                'messages':[{'role':'user','content':'Say one word.'}]})
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith('data: ')]
        gloss_index = next(i for i, event in enumerate(events) if event.get('gloss'))
        text_done_index = next(i for i, event in enumerate(events) if event.get('text_done'))
        self.assertLess(gloss_index, text_done_index)
        self.assertEqual(shared.selected, 1)
        self.assertEqual(next(event for event in events if event.get('done'))['glosses'][0]['gloss'], '银行')

    async def test_failed_change_does_not_save_unapplied_settings(self):
        with patch.object(self.server.engine, "set_parallel", new=AsyncMock(side_effect=RuntimeError("failed"))):
            response = await self.client.post("/api/debug", json={"candidates": 12, "parallel": 2})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.server.debug_settings()["candidates"], 5)
        self.assertEqual(self.server.store.setting("engine_parallel", ""), "1")

    async def test_switch_waits_for_generation_to_finish(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def generate():
            async with self.server.use_engine():
                entered.set()
                await finish.wait()

        generation = asyncio.create_task(generate())
        await entered.wait()
        with patch.object(self.server.engine, "set_parallel", new_callable=AsyncMock) as change:
            request = asyncio.create_task(self.client.post("/api/debug", json={"candidates": 5, "parallel": 2}))
            await asyncio.sleep(0)
            self.assertTrue(self.server._switching.locked())
            change.assert_not_awaited()
            finish.set()
            await generation
            self.assertEqual((await request).status_code, 200)
            change.assert_awaited_once()

    async def test_saved_parallel_is_restored_without_starting_provider(self):
        self.server.store.set_setting("engine_parallel", "2")
        with patch.object(self.server.engine, "models", return_value=[{"name": "external"}]), \
                patch.object(self.server.engine, "aclose", new_callable=AsyncMock) as close:
            async with self.server.lifespan(self.server.app):
                self.assertEqual(self.server.engine.parallel, 2)
            close.assert_awaited_once()

    async def test_external_chat_and_reader_use_real_transport_without_provider_management(self):
        import json
        requests = []
        def respond(request):
            requests.append(request)
            body = json.loads(request.content)
            if request.url.path == '/v1/kv/op':
                return httpx.Response(404, json={'error':{'message':'not supported'}})
            if body.get("stream"):
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"bank"}}]}\n\ndata: [DONE]\n\n')
            keys = body['response_format']['json_schema']['schema']['properties']
            return httpx.Response(200, json={'choices':[{'finish_reason':'stop',
                'message':{'content':json.dumps({key:'B' for key in keys})}}]})
        real_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(self.server.engine, '_client', real_client), \
                patch.object(self.server.dictionary, 'learned_lemmas', return_value=set()), \
                patch.object(self.server.dictionary, 'lemma', side_effect=str.lower), \
                patch.object(self.server.dictionary, 'lookup', return_value={'lemma':'bank','gloss':'银行'}), \
                patch.object(self.server.dictionary, 'senses', return_value=[{'pos':'n','text':'银行'},{'pos':'n','text':'岸'}]), \
                patch.object(self.server, 'model_family', return_value='qwen'):
            response = await self.client.post('/api/chat', json={'messages':[{'role':'user','content':'river'}]})
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            self.assertFalse(any('error' in item for item in events), events)
            done = next(item for item in events if item.get('done'))
            self.assertEqual(done['glosses'][0]['gloss'], '岸')
            self.assertEqual(self.server.store.get(done['conversation_id'])['messages'][-1]['glosses'],done['glosses'])
            result = await self.client.post('/api/reader/gloss',json={'text':'bank'})
            self.assertIn('岸',result.text)
            self.assertIn('"done": true',result.text)
        await real_client.aclose()
        inference = [r for r in requests if r.url.path == '/v1/chat/completions']
        self.assertEqual(len(inference),3)
        self.assertTrue(all(r.url.path in ('/v1/chat/completions','/v1/kv/op') for r in requests))

    async def test_external_truncated_chat_never_saves_partial_reply(self):
        def respond(_):
            return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
        real_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(self.server.engine,'_client',real_client), \
                patch.object(self.server.store,'save') as save:
            response=await self.client.post('/api/chat',json={'messages':[{'role':'user','content':'Hi'}]})
        self.assertIn('error',response.text)
        self.assertNotIn('"done": true',response.text)
        save.assert_not_called()
        await real_client.aclose()

    async def test_model_selection_only_accepts_inventory_and_stale_native_handles_are_rejected(self):
        with patch.object(self.server.engine,'models',return_value=[{'name':'external','family':'qwen','size':0}]):
            self.assertEqual((await self.client.post('/api/model',json={'name':'external'})).status_code,200)
            self.assertEqual(self.server.current_model(),'external')
            self.assertEqual((await self.client.post('/api/model',json={'name':'missing'})).status_code,404)
        self.assertEqual((await self.client.post('/api/gloss',json={
            'text':'bank','sequence_id':'old','cache_epoch':'old','prefix_tokens':1})).status_code,422)

    async def test_unknown_model_template_disables_only_prediction(self):
        with patch.object(self.server,'model_family',return_value='unknown'), \
                patch.object(self.server.engine,'kv_top_tokens',new_callable=AsyncMock) as predict:
            result=await self.client.post('/api/suggest',json={'messages':[],'text':'I '})
            self.assertEqual(result.status_code,503)
            predict.assert_not_awaited()

    async def test_suggest_deadline_cancels_inference_and_releases_admission(self):
        cancelled = asyncio.Event()
        async def blocked(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(self.server, 'SUGGEST_TIMEOUT', 0.03), \
                patch.object(self.server, 'model_family', return_value='qwen3'), \
                patch.object(self.server.predict, 'suggest', side_effect=blocked):
            response = await self.client.post('/api/suggest', json={'messages':[], 'text':'I '})
        self.assertEqual(response.status_code, 504)
        self.assertIn('超时', response.json()['detail'])
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.server._sequence_slot.locked())
        self.assertTrue(self.server._engine_idle.is_set())

    async def test_suggest_deadline_includes_waiting_for_local_sequence(self):
        async with self.server._sequence_slot:
            with patch.object(self.server, 'SUGGEST_TIMEOUT', 0.02), \
                    patch.object(self.server.predict, 'suggest', new_callable=AsyncMock) as predict:
                response = await self.client.post('/api/suggest', json={'messages':[], 'text':'I '})
                self.assertEqual(response.status_code, 504)
                predict.assert_not_awaited()
        self.assertTrue(self.server._engine_idle.is_set())

    async def test_suggest_disconnect_cancels_work_and_next_request_recovers(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def disconnected():
            await started.wait()
            return True
        request = Mock()
        request.is_disconnected = disconnected
        with patch.object(self.server, 'model_family', return_value='qwen3'), \
                patch.object(self.server.predict, 'suggest', side_effect=blocked):
            with self.assertRaises(self.server.HTTPException) as error:
                await self.server.suggest(self.server.SuggestRequest(messages=[], text='I '), request)
        self.assertEqual(error.exception.status_code, 499)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.server._sequence_slot.locked())
        self.assertTrue(self.server._engine_idle.is_set())
        with patch.object(self.server, 'model_family', return_value='qwen3'), \
                patch.object(self.server.predict, 'suggest', new=AsyncMock(return_value=[])):
            self.assertEqual((await self.client.post('/api/suggest', json={'messages':[], 'text':'I '})).json(), [])

    async def test_suggest_provider_error_is_an_actionable_response(self):
        with patch.object(self.server, 'model_family', return_value='qwen3'), \
                patch.object(self.server.predict, 'suggest', side_effect=httpx.ReadTimeout('busy')):
            response = await self.client.post('/api/suggest', json={'messages':[], 'text':'I '})
        self.assertEqual(response.status_code, 504)
        self.assertIn('重试', response.json()['detail'])

    async def test_managed_selection_saves_only_after_provider_success(self):
        previous = self.server.current_model()
        catalog = {'managed':True,'models':[{'name':'new','supported':True},
            {'name':'partial','supported':False,'reason':'未下载完整'}]}
        with patch.object(self.server.engine,'model_catalog',return_value=catalog), \
                patch.object(self.server.engine,'select_external_model',new_callable=AsyncMock) as select:
            self.assertTrue((await self.client.get('/api/models')).json()['managed'])
            self.assertEqual((await self.client.post('/api/model',json={'name':'partial'})).status_code,422)
            select.assert_not_awaited()
            select.side_effect = RuntimeError('加载失败；已恢复原模型')
            failed = await self.client.post('/api/model',json={'name':'new'})
            self.assertEqual(failed.status_code,503)
            self.assertEqual(self.server.current_model(),previous)
            select.side_effect = None
            self.server.engine._ranking = ('stale',[])
            self.assertEqual((await self.client.post('/api/model',json={'name':'new'})).status_code,200)
            self.assertEqual(self.server.current_model(),'new')
            self.assertIsNone(self.server.engine._ranking)
        self.server.store.set_setting('model',previous)

    async def test_model_family_remains_available_during_provider_reload(self):
        with patch.dict(self.server._families,clear=True), \
                patch.object(self.server.engine,'models',side_effect=httpx.ConnectError('loading')), \
                patch.object(self.server.engine,'model_catalog',return_value={
                    'managed':True,'models':[{'name':'new','family':'gemma4'}]}):
            self.assertEqual(self.server.model_family('new'),'gemma4')

    async def test_explicit_model_applies_even_when_provider_is_offline(self):
        import os
        with patch.dict(os.environ,{'ECHO_MODEL':'explicit-model'}), \
                patch.object(config,'MODEL','explicit-model'), \
                patch.object(self.server.engine,'models',side_effect=httpx.ConnectError('offline')), \
                patch.object(self.server.engine,'aclose',new_callable=AsyncMock):
            async with self.server.lifespan(self.server.app):
                self.assertEqual(self.server.current_model(),'explicit-model')
