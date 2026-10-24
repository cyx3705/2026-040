import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock

from echo_app import reader
from echo_app.kv import SequenceHandle


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_remote_prefill_reclaims_late_handle(self):
        from echo_app.kv import RemoteKVBackend
        backend = RemoteKVBackend("http://fixture")
        entered, complete = asyncio.Event(), asyncio.Event()
        async def create(*args):
            entered.set()
            await complete.wait()
            return {"sequence_id": "late", "cache_epoch": "e", "prefix_tokens": 12}
        backend._post = create
        backend.release_sequence = AsyncMock()
        task = asyncio.create_task(backend.create_sequence("m", "page"))
        await entered.wait(); task.cancel(); complete.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        backend.release_sequence.assert_awaited_once_with(SequenceHandle("late", "e", 12))

    def dictionary(self, learned=()):
        dictionary = Mock()
        dictionary.learned_lemmas.return_value = set(learned)
        dictionary.lemma.side_effect = str.lower
        dictionary.lookup.side_effect = lambda w: {"lemma": w.lower(), "gloss": "默认"}
        dictionary.senses.return_value = [{"pos": "n", "text": "银行"}, {"pos": "n", "text": "岸"}]
        return dictionary

    def engine(self):
        engine = Mock()
        engine.kv_create_sequence = AsyncMock(return_value=SequenceHandle("1", "epoch", 42))
        engine.kv_tokenize = AsyncMock(return_value=[1, 2])
        engine.kv_release_sequence = AsyncMock()
        engine.kv.select_batch = AsyncMock(side_effect=lambda sequence, jobs:
            [{"label": " B", "decision_steps": 1} for _ in jobs])
        return engine

    async def collect(self, dictionary, engine, text="bank bank", **kwargs):
        return [copy.deepcopy(e) async for e in reader.events(text, dictionary, engine, "m", "chatml", **kwargs)]

    async def test_all_learned_skips_chinese_and_entire_inference(self):
        dictionary, engine = self.dictionary(["bank"]), self.engine()
        result = await self.collect(dictionary, engine)
        dictionary.lookup.assert_not_called()
        dictionary.senses.assert_not_called()
        engine.kv_tokenize.assert_not_awaited()
        engine.kv_create_sequence.assert_not_awaited()
        self.assertEqual(result[-1]["decisions"], 0)
        self.assertTrue(all(i["state"] == "skipped_learned" for i in result[0]["items"]))

    async def test_input_prefill_once_occurrences_batched_and_released(self):
        dictionary, engine = self.dictionary(), self.engine()
        result = await self.collect(dictionary, engine, "😀 bank bank")
        self.assertEqual([i["start"] for i in result[0]["items"]], [3, 8])
        self.assertEqual(result[0]["items"][0]["state"], "pending")
        engine.kv_create_sequence.assert_awaited_once()
        self.assertIn('😀 bank bank', engine.kv_create_sequence.call_args.args[1])
        engine.kv.select_batch.assert_awaited_once()
        self.assertEqual(len(engine.kv.select_batch.call_args.args[1]), 2)
        self.assertEqual(result[1]["items"][0]["gloss"], "岸")
        self.assertEqual(result[-1]["decisions"], 2)
        engine.kv_release_sequence.assert_awaited_once()

    async def test_single_parallel_and_suffix_budget_respected(self):
        dictionary, engine = self.dictionary(), self.engine()
        await self.collect(dictionary, engine, parallel=1)
        self.assertEqual(engine.kv.select_batch.await_count, 2)
        engine = self.engine()
        engine.kv_tokenize.return_value = list(range(500))
        await self.collect(dictionary, engine)
        self.assertEqual(engine.kv.select_batch.await_count, 2)

    async def test_failure_and_abstention_use_underlined_default_state(self):
        for failure in [False, True]:
            dictionary, engine = self.dictionary(), self.engine()
            engine.kv.select_batch = AsyncMock(side_effect=RuntimeError("broken")) if failure else AsyncMock(
                return_value=[{"label": "0", "decision_steps": 1}] * 2)
            result = await self.collect(dictionary, engine)
            for event in result[1:-1]:
                self.assertEqual(event["items"][0]["state"], "degraded")
                self.assertEqual(event["items"][0]["gloss"], "默认")
            engine.kv_release_sequence.assert_awaited_once()

    async def test_learned_during_selection_never_returns_chinese(self):
        dictionary, engine = self.dictionary(), self.engine()
        async def select(sequence, jobs):
            dictionary.learned_lemmas.return_value = {"bank"}
            return [{"label": " B", "decision_steps": 1}] * len(jobs)
        engine.kv.select_batch.side_effect = select
        result = await self.collect(dictionary, engine)
        self.assertTrue(all(e["items"][0]["gloss"] == "" for e in result[1:-1]))

    async def test_disconnect_and_cancellation_release_input_sequence(self):
        dictionary, engine = self.dictionary(), self.engine()
        checks = AsyncMock(side_effect=[False, True])
        await self.collect(dictionary, engine, disconnected=checks)
        engine.kv.select_batch.assert_not_awaited()
        engine.kv_release_sequence.assert_awaited_once()
        engine = self.engine()
        entered = asyncio.Event()
        async def select(*args):
            entered.set()
            await asyncio.Event().wait()
        engine.kv.select_batch.side_effect = select
        task = asyncio.create_task(self.collect(dictionary, engine))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        engine.kv_release_sequence.assert_awaited_once()

    async def test_oversized_candidates_do_not_silently_drop_choices(self):
        dictionary, engine = self.dictionary(), self.engine()
        dictionary.senses.return_value *= 30
        result = await self.collect(dictionary, engine, "bank")
        self.assertEqual(result[1]["items"][0]["fallback_reason"], "candidate_limit")
        engine.kv_create_sequence.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
