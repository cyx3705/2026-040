"""KV-001 real-model acceptance: a reader session's KV is saved on switch and resumed.

Read-only for the learning library. Uses throwaway session names and removes
their files. Point ECHO_KV_ADAPTER_URL at an idle adapter started from this tree.
"""
import asyncio
import copy
import json
import time
import uuid

from echo_app import config, reader
from echo_app.dictionary import Dictionary
from echo_app.engine import Engine

CUES = [
    "We walked along the river all morning.",
    "The water was calm and the birds were loud.",
    "Then we sat on the bank and ate lunch.",
    "My friend talked about her new job in the city.",
    "She works long hours, but she likes the people.",
    "After lunch we followed the path back to the car.",
    "On the way home we stopped at a small shop.",
    "I bought some bread and a bottle of water.",
]
OTHER = ["She went to the bank to get a loan for her shop.", "The manager said the rate was fair."]


async def gloss(engine, dictionary, model, digest, contexts, text):
    final, start = {}, time.perf_counter()
    async for event in reader.events(text, dictionary, engine, model, "chatml", session=digest, contexts=contexts):
        for item in event.get("items", []):
            final[item["start"]] = copy.deepcopy(item)
    info = await engine.kv.runtime_info()
    return final, round((time.perf_counter() - start) * 1000, 1), info


async def main():
    engine, dictionary = Engine(), Dictionary()
    original = dictionary.learned_lemmas
    dictionary.learned_lemmas = lambda: original() - {"bank"}
    contexts = reader.Contexts(config.KV_SESSION_DIR)
    run, digests = uuid.uuid4().hex[:8], []
    try:
        info = await engine.kv.runtime_info()
        assert info.get("reader_sessions") and info["active_sequences"] == 0, info
        model = info["model"]
        assert model.startswith("qwen"), "chatml acceptance"
        digests = video, page = [reader.session_digest(model, f"acceptance:{run}:{name}") for name in ("video", "page")]
        steps = []
        for text in CUES:
            final, ms, info = await gloss(engine, dictionary, model, video, contexts, text)
            steps.append({"ms": ms, "evaluated": info["session_evaluated_tokens"], "reused": info["session_reused_tokens"]})
        # Only the new cue (plus the short closing turn) is prefilled once context exists.
        assert all(s["reused"] > 0 and s["evaluated"] < 60 for s in steps[1:]), steps
        bank = [i for i in final.values() if i["word"] == "bank"] or \
            [i for i in (await gloss(engine, dictionary, model, video, contexts, CUES[2]))[0].values() if i["word"] == "bank"]
        saves = info["session_saves"]
        final, other_ms, info = await gloss(engine, dictionary, model, page, contexts, OTHER[0])
        assert info["session_saves"] == saves + 1 and info["session_save_failures"] == 0, info
        loan = [i for i in final.values() if i["word"] == "bank"]
        size = (config.KV_SESSION_DIR / f"{video}.kv").stat().st_size
        # Back to the video: its file is loaded, nothing before the new cue is recomputed.
        final, resume_ms, info = await gloss(engine, dictionary, model, video, contexts, "We will go back to the river bank next week.")
        resumed = {"ms": resume_ms, "loaded": info["session_loaded_tokens"], "evaluated": info["session_evaluated_tokens"],
                   "reused": info["session_reused_tokens"]}
        assert resumed["loaded"] > 0 and resumed["reused"] > 0 and resumed["evaluated"] < 60, resumed
        # Cold baseline: the same prompt without a session prefills everything.
        prompt = reader.session_prompt("chatml", contexts.load(video))
        start = time.perf_counter()
        handle = await engine.kv_create_sequence(model, prompt)
        cold_ms = round((time.perf_counter() - start) * 1000, 1)
        await engine.kv_release_sequence(handle)
        river = [i for i in final.values() if i["word"] == "bank"]
        print(json.dumps({"append_steps": steps, "switch_ms": other_ms, "session_file_mb": round(size / 2**20, 1),
                          "resume": resumed, "cold_prefill_ms": cold_ms, "prompt_tokens": handle.prefix_tokens,
                          "glosses": {"walk_bank": [i["gloss"] for i in bank], "loan_bank": [i["gloss"] for i in loan],
                                      "resume_bank": [i["gloss"] for i in river]},
                          "runtime": await engine.kv.runtime_info()}, ensure_ascii=False, indent=2))
    finally:
        for digest in digests:
            for suffix in (".kv", ".json", ".kv.tmp"):
                (config.KV_SESSION_DIR / f"{digest}{suffix}").unlink(missing_ok=True)
        await engine.kv.aclose()


if __name__ == "__main__":
    asyncio.run(main())
