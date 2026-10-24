"""Read-only real-model reader acceptance; never writes learned/user settings."""
import asyncio
import copy
import json
import time

import httpx

from echo_app import reader
from echo_app.dictionary import Dictionary
from echo_app.engine import Engine
from echo_app.predict import template_of

CASES = [
    ("He sat on the bank beside the river. She went to the bank to get a loan.", "bank", [("岸", "堤"), ("银行",)]),
    ("This empty bag is light, not heavy. Please turn on the light in this dark room.", "light", [("轻的",), ("灯", "光源", "发光体")]),
    ("The sound of the bell was loud. This bridge is structurally sound and safe.", "sound", [("声",), ("健", "完好", "坚固")]),
    ("I can book a hotel room online. She is reading a book from the library.", "book", [("预", "订"), ("书",)]),
]


async def main():
    engine = Engine()
    dictionary = Dictionary()
    # Overlay only: ensure target occurrences exercise inference even if learned
    # in the user's real library. Other learned words still skip Chinese lookup.
    original_learned = dictionary.learned_lemmas
    targets = {case[1] for case in CASES}
    dictionary.learned_lemmas = lambda: original_learned() - targets
    try:
        before = await engine.kv.runtime_info()
        assert before["active_sequences"] == before["active_branches"] == 0, "Keep Echo idle during acceptance"
        assert before.get("input_batch_max") == 2
        model = before["model"]
        template = "chatml" if model.startswith("qwen") else template_of(model)
        assert template, "Unsupported template"
        metrics = []
        for parallel in (1, 2):
            for text, target, expected in CASES:
                start = time.perf_counter()
                final, first_ms, done = {}, None, None
                async for event in reader.events(text, dictionary, engine, model, template, parallel=parallel):
                    if first_ms is None:
                        first_ms = (time.perf_counter() - start) * 1000
                    for item in event.get("items", []):
                        final[item["start"]] = copy.deepcopy(item)
                    if event.get("done"):
                        done = event
                found = [item for _, item in sorted(final.items()) if item["word"].lower() == target]
                assert len(found) == len(expected), found
                for item, meaning in zip(found, expected):
                    assert item["state"] == "normal" and any(m in item["gloss"] for m in meaning), (text, item, meaning)
                    assert item["decision_steps"] == 1, item
                metrics.append({"parallel": parallel, "target": target, "dictionary_ms": round(first_ms, 1),
                                "total_ms": round((time.perf_counter() - start) * 1000, 1),
                                "glosses": [i["gloss"] for i in found], **done})
        # Actual HTTP contract also streams an initial dictionary event first.
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post("http://127.0.0.1:8040/api/reader/gloss", json={"text": CASES[0][0]})
            response.raise_for_status()
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert events[0]["phase"] == "dictionary" and events[-1]["done"]
        after = await engine.kv.runtime_info()
        assert after["active_sequences"] == after["active_branches"] == 0, after
        assert after["fork_count"] == after["release_count"], after
        # Cancel while a real HTTP prefill can still be running. A late handle
        # must be retained and released before another source is admitted.
        for _ in range(3):
            pending = asyncio.create_task(engine.kv_create_sequence(model, "The river bank is quiet. " * 200))
            await asyncio.sleep(.005)
            pending.cancel()
            try:
                handle = await pending
            except asyncio.CancelledError:
                pass
            else:
                await engine.kv_release_sequence(handle)
            after = await engine.kv.runtime_info()
            assert after["active_sequences"] == after["active_branches"] == 0, after
        print(json.dumps({"passed_target_occurrences": 16, "cancelled_prefill_cycles": 3,
                          "metrics": metrics, "runtime": after}, ensure_ascii=True, indent=2))
    finally:
        await engine.kv.aclose()


if __name__ == "__main__":
    asyncio.run(main())
