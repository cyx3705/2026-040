"""DEBUG-001/002：调试参数校验与保存，安全重启私有引擎，并发切换的失败恢复。"""

import asyncio
import hashlib
import importlib
import re
import tempfile
import unittest
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


class NativeKernelTests(unittest.TestCase):
    def test_input_dll_lifecycle_without_an_active_focus_lease(self):
        from echo_app.input_bridge import InputBridge
        bridge = InputBridge()
        try:
            bridge._start()
            self.assertFalse(bridge.refresh(999999)["active"])
            bridge.release(999999)  # Stale handles cannot acquire focus or change layouts.
        finally:
            bridge.close()
            bridge.close()

    def test_cpp_runtime_contract_without_gpu(self):
        # These invariants now execute inside the production C++ Runtime with
        # an injected llama ABI, replacing mocks of removed Python internals.
        import subprocess
        from echo_app.kernel_build import ensure_built
        executable = ensure_built().with_name("kernel_tests.exe")
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("PASS "), 15, result.stdout)
        policy = subprocess.run([str(executable.with_name("input_tests.exe"))], capture_output=True, text=True, timeout=60)
        self.assertEqual(policy.returncode, 0, policy.stdout + policy.stderr)


class EngineParallelTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_rejects_legacy_python_kernel(self):
        engine = Engine()
        with patch.object(config, "KV_ADAPTER_URL", "http://native"), \
                patch.object(engine.kv, "runtime_info", new=AsyncMock(return_value={
                    "shared_chat": True, "top_tokens": True, "model": "m"})), \
                patch.object(engine, "unload", new_callable=AsyncMock) as unload:
            with self.assertRaisesRegex(RuntimeError, "旧版 Python 内核"):
                await engine.start_kv("m")
        unload.assert_not_awaited()

    async def test_native_parallel_changes_scheduling_without_loading_second_model(self):
        engine = Engine()
        with patch.object(config, "KV_ADAPTER_URL", "http://native"), \
                patch.object(engine.kv, "runtime_info", new=AsyncMock(return_value={"shared_chat": True, "model": "m"})), \
                patch.object(engine, "load", new_callable=AsyncMock) as load, \
                patch.object(engine, "stop") as stop:
            await engine.set_parallel(2, "m")
        self.assertEqual(engine.parallel, 2)
        load.assert_not_awaited()
        stop.assert_not_called()

    async def test_capability_rejects_different_runtime_model(self):
        engine = Engine()
        with patch.object(engine.kv, "runtime_info", new=AsyncMock(return_value={"shared_chat": True, "top_tokens": True, "model": "wrong"})), \
                patch.object(engine.kv, "capabilities", new=AsyncMock(return_value=KVCapabilities("native", True, True, True, True, True))):
            result = await engine.kv_capabilities("requested")
        self.assertFalse(result.supported)
        self.assertEqual(result.reason, "model_mismatch")

    async def test_change_restarts_and_reloads_current_model(self):
        engine = Engine()
        engine._proc = Mock()
        engine._proc.poll.return_value = None
        with patch.object(config, "KV_ADAPTER_URL", None), \
                patch.object(engine, "running", return_value=True), \
                patch.object(engine, "start") as start, patch.object(engine, "stop") as stop, \
                patch.object(engine, "load", new_callable=AsyncMock) as load:
            await engine.set_parallel(2, "gemma4:12b")
            self.assertEqual(engine.parallel, 2)
            start.assert_called_once()
            stop.assert_called_once()
            load.assert_awaited_once_with("gemma4:12b")

    async def test_failed_load_restores_previous_slots_and_model(self):
        engine = Engine()
        engine._proc = Mock()
        engine._proc.poll.return_value = None
        with patch.object(config, "KV_ADAPTER_URL", None), \
                patch.object(engine, "running", return_value=True), patch.object(engine, "start"), \
                patch.object(engine, "stop"), \
                patch.object(engine, "load", new=AsyncMock(side_effect=[RuntimeError("load failed"), None])) as load:
            with self.assertRaisesRegex(RuntimeError, "load failed"):
                await engine.set_parallel(2, "gemma4:12b")
            self.assertEqual(engine.parallel, 1)
            self.assertEqual(load.await_count, 2)

    async def test_does_not_stop_an_engine_owned_by_another_process(self):
        engine = Engine()
        with patch.object(config, "KV_ADAPTER_URL", None), \
                patch.object(engine, "running", return_value=True), patch.object(engine, "stop") as stop:
            with self.assertRaises(RuntimeError):
                await engine.set_parallel(2, "qwen3:8b")
            stop.assert_not_called()


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
                patch.object(self.server.engine, "kv_create_sequence", new_callable=AsyncMock) as create:
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

    async def test_native_stream_glosses_before_text_done_persists_and_releases(self):
        import json
        sequence = SequenceHandle("a-unique", "e", 100)
        self.server.engine.parallel = 2

        async def chat(*args, on_sequence, **kwargs):
            on_sequence(sequence)
            for piece in ("A river bank ", "is peaceful. ", "Do you agree?"):
                yield piece
                await asyncio.sleep(0.02)

        async def prefix(seq, text):
            self.assertEqual(seq, sequence)
            if text.endswith("bank"):
                self.assertEqual(text, "A river bank")
            return 102, ""

        with patch.object(self.server.engine, "chat", side_effect=chat), \
                patch.object(self.server.engine, "kv_capabilities", new=AsyncMock(
                    return_value=KVCapabilities("native", True, True, True, True, True))), \
                patch.object(self.server, "model_family", return_value="gemma4"), \
                patch.object(self.server.engine, "kv_prefix_for_text", side_effect=prefix), \
                patch.object(self.server.engine, "kv_tokenize", new=AsyncMock(return_value=[1])), \
                patch.object(self.server.engine, "fork_at", new=AsyncMock(return_value=BranchHandle("b", "a", "e"))), \
                patch.object(self.server.engine, "decode_label", new=AsyncMock(
                    side_effect=lambda branch, suffix, labels, budget: labels[1])), \
                patch.object(self.server.engine, "release_branch", new=AsyncMock()) as release_b, \
                patch.object(self.server.engine, "kv_release_sequence", new=AsyncMock()) as release_a:
            response = await self.client.post("/api/chat", json={"messages": [{"role": "user", "content": "river"}]})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertFalse(any("error" in event for event in events), events)
        text_done = next(i for i, event in enumerate(events) if event.get("text_done"))
        self.assertTrue(any(event.get("gloss") for event in events[:text_done]))
        done = next(event for event in events if event.get("done"))
        stored = self.server.store.get(done["conversation_id"])["messages"][-1]
        self.assertEqual(stored["glosses"], done["glosses"])
        release_a.assert_awaited_once_with(sequence)
        self.assertGreater(release_b.await_count, 0)

    async def test_history_default_never_creates_a_or_requests_model(self):
        with patch.object(self.server.engine, "ask", new_callable=AsyncMock) as ask, \
                patch.object(self.server.engine, "kv_create_sequence", new_callable=AsyncMock) as create:
            response = await self.client.post("/api/gloss", json={"text": "bank", "default_only": True})
        self.assertEqual(response.json()[0]["fallback_reason"], "legacy_default")
        ask.assert_not_awaited()
        create.assert_not_awaited()

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

    async def test_input_translation_selects_one_label_and_always_releases(self):
        sequence = SequenceHandle("translate", "epoch", 80)
        branch = BranchHandle("choice", "translate", "epoch")
        with patch.object(self.server.dictionary, "english_choices", return_value=("learn", "study")), \
                patch.object(self.server.engine, "kv_capabilities", new=AsyncMock(return_value=
                    KVCapabilities("native", True, True, True, True, True))), \
                patch.object(self.server, "model_family", return_value="qwen3"), \
                patch.object(self.server.engine, "kv_create_sequence", new=AsyncMock(return_value=sequence)) as create, \
                patch.object(self.server.engine, "fork_at", new=AsyncMock(return_value=branch)), \
                patch.object(self.server.engine, "kv_tokenize", new=AsyncMock(return_value=[1, 2])), \
                patch.object(self.server.engine, "decode_label", new=AsyncMock(side_effect=[" B", "0", RuntimeError("fail")])) as select, \
                patch.object(self.server.engine, "release_branch", new=AsyncMock()) as release_b, \
                patch.object(self.server.engine, "kv_release_sequence", new=AsyncMock()) as release_a, \
                patch.object(self.server.engine, "ask", new=AsyncMock()) as ask:
            payload = {"messages": [], "chinese": "学习", "before": "I ", "after": " English"}
            first = await self.client.post("/api/input-translation", json=payload)
            self.assertEqual(first.json(), {"word": "study", "reason": None})
            self.assertIn('"before": "I "', create.call_args.args[1])
            self.assertEqual(select.call_args.args[2], ["0", " A", " B"])
            self.assertEqual(select.call_args.kwargs, {"budget": 1})
            abstain = await self.client.post("/api/input-translation", json=payload)
            self.assertEqual(abstain.json()["reason"], "unresolved")
            failed = await self.client.post("/api/input-translation", json=payload)
            self.assertEqual(failed.json()["reason"], "selector_failure")
            self.assertEqual(release_a.await_count, 3)
            self.assertEqual(release_b.await_count, 3)
            ask.assert_not_awaited()

    async def test_input_translation_missing_choices_never_runs_model(self):
        with patch.object(self.server.dictionary, "english_choices", return_value=()), \
                patch.object(self.server.engine, "kv_create_sequence", new=AsyncMock()) as create:
            for term, reason in [("不存在的词", "no_dictionary_choices"), ("hello", "chinese_word_required")]:
                response = await self.client.post("/api/input-translation", json={"messages": [], "chinese": term})
                self.assertEqual(response.json(), {"word": None, "reason": reason})
            create.assert_not_awaited()

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

    async def test_saved_parallel_is_restored_on_startup(self):
        self.server.store.set_setting("engine_parallel", "2")
        with patch.object(Engine, "installed", new_callable=PropertyMock, return_value=True), \
                patch.object(self.server.engine, "start") as start, patch.object(self.server.engine, "stop"), \
                patch.object(self.server.engine, "start_kv", new=AsyncMock()) as start_kv, \
                patch.object(self.server.engine, "_stop_kv"):
            async with self.server.lifespan(self.server.app):
                self.assertEqual(self.server.engine.parallel, 2)
                start.assert_called_once()
                start_kv.assert_awaited_once_with(self.server.current_model())
