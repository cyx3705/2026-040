"""PRED-001：每次提示只请求一次模型，保持过滤、去重、标点和半词补全。"""

import json
import unittest
from unittest.mock import patch

import httpx

from echo_app import predict
from echo_app.engine import Engine
from echo_app.dictionary import sense_candidates
from echo_app.dictionary import Dictionary
import sqlite3


class SensePromptBudgetTests(unittest.TestCase):
    def test_compact_prompt_keeps_all_explicit_choices_and_abstention(self):
        task = {"word": "light", "candidates": [
            {"label": "轻的", "pos": "a.", "meaning": "轻的"},
            {"label": "灯", "pos": "n.", "meaning": "灯"}],
            "allowed_labels": ["0", "轻的", "灯"], "left_context": "This bag is light"}
        for compact in (True, "grouped"):
            prompt = predict.sense_suffix("chatml", "", task, compact=compact)
            self.assertEqual(prompt.count("轻的"), 1)
            self.assertEqual(prompt.count("灯"), 1)
            self.assertIn('"label": "0"', prompt)
            self.assertTrue(prompt.endswith("Label:"))
            self.assertNotIn('"allowed_labels"', prompt)
        self.assertEqual(task["allowed_labels"], ["0", "轻的", "灯"])


class PredictionTests(unittest.IsolatedAsyncioTestCase):
    async def suggestions(self, tokens, text, known=(), complete=None, count=5):
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json={"completion_probabilities": [{"top_logprobs": [
                {"token": token, "logprob": -i - 1} for i, token in enumerate(tokens)
            ]}]})

        words = {w.lower() for w in known}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(Engine, "client", new=property(lambda self: client)):
                items = await predict.suggest(
                    "qwen3:8b", "chatml", "Speak English.", [], text,
                    complete=complete, is_word=lambda w: w.lower() in words, k=count,
                )
        self.assertEqual(len(requests), 1, "筛词或补全不得增加模型请求")
        self.assertEqual(requests[0]["n_predict"], 1)
        self.assertEqual(requests[0]["n_probs"], 20)
        self.assertTrue(requests[0]["cache_prompt"])
        return items, requests[0]

    async def test_filters_tokens_and_keeps_five_unique_words_in_model_order(self):
        items, _ = await self.suggestions(
            ["ing", "<think>", " ", " foo_bar", " spic", " English", " english",
             " how", " to", " more", " about", " some"],
            "I want to learn ", ["English", "how", "to", "more", "about", "some"],
        )
        self.assertEqual([i["word"] for i in items], ["English", "how", "to", "more", "about"])
        self.assertTrue(all(i["kind"] == "word" for i in items))

    async def test_sentence_start_accepts_unspaced_capitalized_words(self):
        items, _ = await self.suggestions(["sem", "I", "hello", "Hello", "."], "", ["sem", "I", "hello"])
        self.assertEqual([i["word"] for i in items], ["I", "Hello"])

    async def test_punctuation_does_not_require_dictionary_entry(self):
        items, _ = await self.suggestions([".", " ,", "?", "!", " ing"], "I agree ")
        self.assertEqual([i["word"] for i in items], [".", ",", "?", "!"])
        self.assertTrue(all(i["kind"] == "punct" for i in items))

    async def test_partial_word_uses_dictionary_without_extra_model_call(self):
        calls = []

        def complete(prefix, count):
            calls.append((prefix, count))
            return ["make", "maker", "makeup", "making", "makeshift"]

        items, request = await self.suggestions(
            [" make", " ma", " other", "."], "I want to mak", ["make", "ma", "other"], complete,
        )
        self.assertEqual([i["word"] for i in items], ["make", "maker", "makeup", "making", "makeshift"])
        self.assertEqual(calls, [("mak", 10)])
        self.assertTrue(request["prompt"].endswith("I want to"))
        self.assertEqual([i["kind"] for i in items], ["word", "dict", "dict", "dict", "dict"])

    async def test_partial_word_preserves_typed_case_for_both_sources(self):
        items, _ = await self.suggestions(
            [" sichuan", " Sicily"], "I live in Sic", ["Sichuan", "Sicily"],
            lambda prefix, count: ["sichuan", "sicily", "sick"],
        )
        self.assertEqual([i["word"] for i in items], ["Sichuan", "Sicily", "Sick"])

    async def test_insufficient_candidates_do_not_trigger_model_expansion(self):
        items, _ = await self.suggestions([" frag", " ing"], "I enjoy ")
        self.assertEqual(items, [])

    async def test_selected_count_limits_model_candidates(self):
        words = ["one", "two", "three", "four", "five", "six", "seven", "eight"]
        for count in (1, 3, 8, 20):
            with self.subTest(count=count):
                items, _ = await self.suggestions([" " + w for w in words], "I count ", words, count=count)
                self.assertEqual([i["word"] for i in items], words[:count])

    async def test_selected_count_also_limits_dictionary_fallback(self):
        items, _ = await self.suggestions(
            [], "I want to mak", complete=lambda prefix, count: ["make", "maker", "making", "makeup"], count=3,
        )
        self.assertEqual([i["word"] for i in items], ["make", "maker", "making"])


class DictionarySenseTests(unittest.TestCase):
    def test_reverse_lookup_prefers_exact_senses_and_includes_curated_greetings(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE words(word, translation, frq)")
        con.executemany("INSERT INTO words VALUES(?,?,?)", [
            ("cider", "n. 苹果汁", 1), ("apple", "n. 苹果, 苹果树", 2),
            ("apple;DROP", "n. 苹果", 1), ("APple", "n. 苹果", 3),
            ("nihao", "你好（中文拼音）", 0),
            ("How do you do", "你好", 0)])
        dictionary = object.__new__(Dictionary)
        with patch.object(Dictionary, "installed", True), patch.object(dictionary, "_con", return_value=con):
            self.assertEqual(dictionary.english_choices("苹果"), ("apple", "cider"))
            self.assertIn("hello", dictionary.english_choices("你好"))
            self.assertNotIn("nihao", dictionary.english_choices("你好"))
            self.assertIn("How do you do", dictionary.english_choices("你好"))
            self.assertEqual(dictionary.english_choices("%"), ())
            self.assertEqual(dictionary.english_choices("_"), ())

    def test_full_sense_parser_keeps_all_comma_items(self):
        self.assertEqual(
            sense_candidates("n. 盘子, 碟, 菜肴\n[医] 盔, 盘"),
            [{"pos": "n.", "text": "盘子"}, {"pos": "n.", "text": "碟"},
             {"pos": "n.", "text": "菜肴"}, {"pos": "", "text": "[医] 盔"},
             {"pos": "", "text": "[医] 盘"}],
        )

    def test_full_sense_parser_preserves_nested_parentheses(self):
        self.assertEqual(sense_candidates("n. 岸（河, 湖（包括池塘, 水库））, 银行"),
                         [{"pos": "n.", "text": "岸（河, 湖（包括池塘, 水库））"},
                          {"pos": "n.", "text": "银行"}])

    def test_full_sense_parser_deduplicates_same_pos_and_text(self):
        self.assertEqual(
            sense_candidates("n. 银行, 岸\nadv. 岸\nn. 银行"),
            [{"pos": "n.", "text": "银行"}, {"pos": "n.", "text": "岸"},
             {"pos": "adv.", "text": "岸"}],
        )


if __name__ == "__main__":
    unittest.main()
