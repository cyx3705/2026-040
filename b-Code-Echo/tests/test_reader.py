"""EXT-001/WEB-009: external batch selection and text context, no model KV."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from echo_app import reader


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def dictionary(self, learned=()):
        d = Mock()
        d.learned_lemmas.return_value = set(learned)
        d.lemma.side_effect = str.lower
        d.lookup.side_effect = lambda w: {'lemma':w.lower(), 'gloss':'默认'}
        d.senses.return_value = [{'pos':'n','text':'银行'},{'pos':'n','text':'岸'}]
        return d

    def engine(self):
        e = Mock()
        async def choose(model, prompt, **kwargs):
            return json.dumps({key:'B' for key in kwargs['format_schema']['properties']})
        e.ask = AsyncMock(side_effect=choose)
        return e

    async def collect(self, d, e, text='bank bank', **kw):
        return [copy.deepcopy(x) async for x in reader.events(text,d,e,'m','chatml',**kw)]

    async def test_learned_words_skip_dictionary_and_model(self):
        d,e = self.dictionary(['bank']),self.engine()
        events = await self.collect(d,e)
        d.lookup.assert_not_called()
        e.ask.assert_not_awaited()
        self.assertEqual(events[-1]['decisions'],0)
        self.assertTrue(all(i['state']=='skipped_learned' for i in events[0]['items']))

    async def test_batch_occurrences_have_utf16_offsets_and_validated_labels(self):
        d,e = self.dictionary(),self.engine()
        events = await self.collect(d,e,'😀 bank bank')
        self.assertEqual([i['start'] for i in events[0]['items']],[3,8])
        e.ask.assert_awaited_once()
        self.assertIn('😀 bank bank',e.ask.call_args.args[1])
        self.assertEqual([x['items'][0]['gloss'] for x in events[1:-1]],['岸','岸'])
        self.assertEqual(events[-1]['input_tokens'],0)
        self.assertNotIn('decision_steps',events[1]['items'][0])

    async def test_batch_bound_is_twelve(self):
        e=self.engine()
        await self.collect(self.dictionary(),e,' '.join(['bank']*25))
        self.assertEqual([len(c.kwargs['format_schema']['properties']) for c in e.ask.call_args_list],[12,12,1])

    async def test_bad_labels_failures_and_abstention_retain_default(self):
        for result in ['{"0":"0"}','{"0":"Z"}','{"0":true}', '[]','garbage',RuntimeError('offline')]:
            e=self.engine()
            e.ask.side_effect=result if isinstance(result,Exception) else None
            e.ask.return_value=result
            events=await self.collect(self.dictionary(),e,'bank')
            item=events[-2]['items'][0]
            self.assertEqual(item['state'],'degraded')
            self.assertEqual(item['gloss'],'默认')
            self.assertTrue(events[-1]['done'])

    async def test_learning_during_selection_discards_chinese(self):
        d,e=self.dictionary(),self.engine()
        async def select(*args,**kwargs):
            d.learned_lemmas.return_value={'bank'}
            return '{"0":"B","1":"B"}'
        e.ask.side_effect=select
        events=await self.collect(d,e)
        self.assertTrue(all(x['items'][0]['state']=='skipped_learned' for x in events[1:-1]))

    async def test_disconnect_skips_request_and_cancel_propagates(self):
        e=self.engine()
        await self.collect(self.dictionary(),e,disconnected=AsyncMock(return_value=True))
        e.ask.assert_not_awaited()
        entered=asyncio.Event()
        async def select(*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
        e.ask.side_effect=select
        task=asyncio.create_task(self.collect(self.dictionary(),e))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_text_context_persists_and_is_bounded_without_kv(self):
        with tempfile.TemporaryDirectory() as directory:
            contexts=reader.Contexts(Path(directory))
            session=reader.session_digest('m','yt:video')
            e=self.engine()
            await self.collect(self.dictionary(),e,'river bank',session=session,contexts=contexts)
            await self.collect(self.dictionary(),e,'bank loan',session=session,contexts=contexts)
            self.assertIn('river bank',e.ask.call_args.args[1])
            self.assertEqual(contexts.load(session),['river bank','bank loan'])
            self.assertEqual(len(list(Path(directory).glob('*.json'))),1)
            self.assertEqual(list(Path(directory).glob('*.kv')),[])
            contexts.save(session,['bank'*400]*64)
            await self.collect(self.dictionary(),e,'bank',session=session,contexts=contexts)
            self.assertLessEqual(sum(map(len,contexts.load(session))),8000)

    async def test_unknown_translation_batches_and_validates_output(self):
        d,e=self.dictionary(),self.engine()
        d.lookup.side_effect=None
        d.lookup.return_value=None
        sizes=[]
        async def chat(model,system,messages,**kwargs):
            targets=json.loads(messages[0]['content'])['targets']
            sizes.append(len(targets))
            yield json.dumps({key:'新词' for key in targets})
        e.chat=Mock(side_effect=chat)
        events=await self.collect(d,e,' '.join(['foobar']*13))
        self.assertEqual(sizes,[12,1])
        self.assertTrue(all(x['items'][0]['source']=='model' for x in events[1:-1]))
        e.ask.assert_not_awaited()

    async def test_unknown_failure_and_learned_race(self):
        for output,learned,expected in [('{}',False,'degraded'),('{"0":"English"}',False,'degraded'),
                                       ('not-json',False,'degraded'),('{"0":"新词"}',True,'skipped_learned')]:
            d,e=self.dictionary(),self.engine()
            d.lookup.side_effect=None
            d.lookup.return_value=None
            async def chat(*args,**kwargs):
                if learned:
                    d.learned_lemmas.return_value={'foobar'}
                yield output
            e.chat=Mock(side_effect=chat)
            events=await self.collect(d,e,'foobar')
            self.assertEqual(events[-2]['items'][0]['state'],expected)
