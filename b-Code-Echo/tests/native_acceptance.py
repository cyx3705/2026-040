"""SENSE-002/006 live acceptance, explicitly run against the current native GPU service.

Does not modify conversations, learned words or settings. Unit-test discovery
does not run this GPU check. Use --cycles 1000 for the lifecycle soak test.
"""
import argparse
import asyncio
import json
import math
import statistics
import tempfile
import time
from pathlib import Path

import httpx

from echo_app import config, predict
from echo_app.dictionary import Dictionary
from echo_app.kv import RemoteKVBackend
from echo_app.kv import SequenceHandle


CASES = [
    ("river_bank", "", "We walked beside the river and rested on its bank", "bank", {"岸", "堤"}),
    ("financial_bank", "", "I deposited my savings at the bank", "bank", {"银行"}),
    ("bank_unclear", "", "The bank", "bank", {"0"}),
    ("like_preposition", "", "It looks like", "like", {"象", "像", "如同"}),
    ("like_verb", "", "I really like", "like", {"喜欢"}),
    ("dish_food", "", "The chef cooked three delicious dishes", "dishes", {"菜肴"}),
    ("dish_plate", "", "After dinner I washed the dirty dishes", "dishes", {"盘子", "碟"}),
    ("missing_phrasal_sense", "Express relying on your friend, not a financial transaction.",
     "I can bank", "bank", {"0"}),
    ("sound_incomplete", "", "That sounds", "sounds", {"0"}),
    ("sound_noun", "", "I heard a strange sound", "sound", {"声音"}),
    ("bill_payment", "", "The waiter brought the bill", "bill", {"帐单", "账单"}),
    ("bill_bird", "", "The bird has a yellow bill", "bill", {"鸟嘴"}),
    ("light_weight", "", "This suitcase is very light", "light", {"轻的"}),
    ("light_lamp", "", "She switched on the light", "light", {"灯"}),
]


async def prediction_cache_acceptance(backend, model, template):
    """PERF-004: real GPU cache hits, token-tail edits and clean handoff to A."""
    system = config.system_prompt("cefr:A2")
    texts = ["I want", "I wanted", "I want to", "I", "We would like", "We would"]
    previous = predict.render(template, system, [], "I")
    checked = []
    for text in texts:
        prompt = predict.render(template, system, [], text)
        await backend.top_tokens(previous, 20)
        warm = await backend.top_tokens(prompt, 20)
        info = await backend.runtime_info()
        assert info["active_sequences"] == info["active_branches"] == 0
        assert 0 < info["prediction_cached_tokens"] <= info["context_size"] - 768
        assert await backend.top_tokens(prompt, 20) == warm
        hit = await backend.runtime_info()
        assert hit["prediction_evaluated_tokens"] == 0
        assert hit["prediction_reused_tokens"] == hit["prediction_cached_tokens"]
        sequence = await backend.create_sequence(model, "Reset idle prediction KV.")
        try:
            active = await backend.runtime_info()
            assert active["prediction_cached_tokens"] == 0 and active["active_sequences"] == 1
        finally:
            await backend.release_sequence(sequence)
        cold = await backend.top_tokens(prompt, 20)
        # The bundled quantized GPU kernels differ between full prefill and
        # incremental batches. Near ties can swap, so compare distributions.
        # The fake-ABI test separately checks exact KV contents and cold equality.
        cold_scores = {item["token"]: item["logprob"] for item in cold}
        shared = [item for item in warm if item["token"] in cold_scores]
        assert len(shared) >= 18, (text, warm, cold)
        drift = max(abs(item["logprob"] - cold_scores[item["token"]]) for item in shared)
        warm_scores = {item["token"]: item["logprob"] for item in warm}
        variation = .5 * sum(abs((math.exp(warm_scores[t]) if t in warm_scores else 0) -
                                   (math.exp(cold_scores[t]) if t in cold_scores else 0))
                             for t in warm_scores.keys() | cold_scores.keys())
        assert drift < .35 and variation < .075, (text, drift, variation)
        checked.append({"text": text, "reused": info["prediction_reused_tokens"],
                        "evaluated": info["prediction_evaluated_tokens"], "max_logprob_drift": round(drift, 5),
                        "probability_variation": round(variation, 5)})
        previous = prompt
    # Following semantic cases must start with no idle prediction allocation.
    sequence = await backend.create_sequence(model, "Reset prediction acceptance.")
    await backend.release_sequence(sequence)
    report = {"prediction_cache": checked, "exact_repeat_no_forward": True, "A_clears_cache": True}
    print(json.dumps(report), flush=True)
    return report


async def generation_isolation(backend, model, template, branch_suffix=None):
    prompt = predict.assistant_prompt(template, config.system_prompt("cefr:A2"),
                                     [{"role": "user", "content": "Talk about walking in a park."}])

    async def generate(with_branch):
        sequence = None
        branch_task = None
        text, arrivals = [], []
        started = time.perf_counter()

        async def interfere():
            branch = await backend.fork_at(sequence, sequence.prefix_tokens)
            try:
                suffix = await backend.tokenize(branch_suffix or "\nDictionary task. Choose A or B. Label:", False)
                await backend.decode_label(branch, suffix, ["0", "A", "B"], 8)
                # Successful B must free its cells before returning the label.
                # The explicit release below remains required on error/cancel.
                assert (await backend.runtime_info())["active_branches"] == 0
            finally:
                await backend.release(branch)

        try:
            async with backend.client.stream("POST", backend.base_url + "/v1/kv/chat", json={
                "model": model, "prompt": prompt, "max_tokens": 32, "seed": 73,
            }) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    if "error" in event:
                        raise AssertionError(event["error"])
                    if "sequence" in event:
                        sequence = SequenceHandle(**event["sequence"])
                    if "delta" in event:
                        text.append(event["delta"])
                        arrivals.append(time.perf_counter() - started)
                        if with_branch and branch_task is None:
                            branch_task = asyncio.create_task(interfere())
            if branch_task:
                await branch_task
            gaps = [(b-a)*1000 for a, b in zip(arrivals, arrivals[1:])]
            return "".join(text), {"first_ms": round(arrivals[0]*1000, 1),
                                   "gap_p50_ms": round(statistics.median(gaps), 1),
                                   "gap_p95_ms": round(sorted(gaps)[int((len(gaps)-1)*.95)], 1),
                                   "total_ms": round((time.perf_counter()-started)*1000, 1)}
        finally:
            if branch_task and not branch_task.done():
                branch_task.cancel()
                await asyncio.gather(branch_task, return_exceptions=True)
            if sequence:
                await backend.release_sequence(sequence)

    baseline, plain_metrics = await generate(False)
    branched, fork_metrics = await generate(True)
    assert baseline == branched, (baseline, branched)
    report = {"same_seed_same_A": True, "baseline": plain_metrics, "with_B": fork_metrics}
    print(json.dumps(report), flush=True)
    return report


async def run(url: str, cycles: int) -> dict:
    backend = RemoteKVBackend(url, timeout=60)
    results = []
    try:
        info = await backend.runtime_info()
        if info["active_sequences"] or info["active_branches"]:
            raise RuntimeError("The native runtime is busy. Finish the active conversation first.")
        assert info.get("kernel") == "cpp", "Generation/prediction must use the C++ kernel"
        model = info["model"]
        template = predict.template_of(model.partition(":")[0])
        if template is None:
            raise RuntimeError("Unsupported template for acceptance")
        prediction_report = await prediction_cache_acceptance(backend, model, template)
        # Isolate even the dictionary's user-DB connection from user data.
        with tempfile.TemporaryDirectory() as directory:
            original = config.USER_DB
            config.USER_DB = Path(directory) / "user.db"
            dictionary = Dictionary()
            config.USER_DB = original
            try:
                for name, context, prefix, word, expected in CASES:
                    messages = [{"role": "user", "content": context}] if context else []
                    prompt = predict.assistant_prompt(template, config.system_prompt("cefr:A2"), messages)
                    sequence = await backend.create_sequence(model, prompt + prefix)
                    branch = None
                    started = time.perf_counter()
                    try:
                        senses = dictionary.senses(word)
                        candidates = [{"label": item["text"],
                                       "pos": item["pos"], "meaning": item["text"]}
                                      for i, item in enumerate(senses)]
                        labels = list(dict.fromkeys(["0", *[item["label"] for item in candidates]]))
                        task = {"word": word, "candidates": candidates, "allowed_labels": labels,
                                "left_context": prefix[-160:], "lemma": dictionary.lemma(word),
                                "occurrence_id": "test-target"}
                        suffix = await backend.tokenize(predict.sense_suffix(template, "", task), False)
                        original_suffix_tokens = len(suffix)
                        if len(suffix) > 640:
                            suffix = await backend.tokenize(predict.sense_suffix(template, "", task, compact=True), False)
                        if len(suffix) > 640:
                            suffix = await backend.tokenize(predict.sense_suffix(template, "", task, compact="grouped"), False)
                        branch = await backend.fork_at(sequence, sequence.prefix_tokens)
                        shared = await backend.runtime_info()
                        assert shared["b_shared_positions"] == sequence.prefix_tokens
                        assert shared["logical_unique_positions"] == sequence.prefix_tokens
                        label = await backend.decode_label(branch, suffix, labels, 16)
                        meaning = "0" if label == "0" else next(c["meaning"] for c in candidates if c["label"] == label)
                        result = {"case": name, "label": label, "meaning": meaning,
                                  "expected": sorted(expected), "pass": meaning in expected,
                                  "original_suffix_tokens": original_suffix_tokens,
                                  "suffix_tokens": len(suffix),
                                  "ms": round((time.perf_counter() - started)*1000, 1)}
                        results.append(result)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                    finally:
                        if branch:
                            await backend.release(branch)
                        await backend.release_sequence(sequence)
            finally:
                dictionary._user.close()

        sequence = await backend.create_sequence(model, "A shared prefix with a right-side continuation.")
        base = await backend.runtime_info()
        suffix = await backend.tokenize(" Label:", False)
        soak_start = time.perf_counter()
        try:
            for i in range(cycles):
                # Historical prefix, never the whole later A text.
                count = min(sequence.prefix_tokens, 4 + i % max(1, sequence.prefix_tokens - 3))
                branch = await backend.fork_at(sequence, count)
                try:
                    await backend.decode_label(branch, suffix, ["0", "A", "B"], 8)
                finally:
                    await backend.release(branch)
                    await backend.release(branch)  # Idempotence is part of the contract.
                if (i+1) % 100 == 0:
                    state = await backend.runtime_info()
                    assert state["active_branches"] == 0 and state["a_tokens"] == base["a_tokens"]
                    print(json.dumps({"cycles": i+1, "seconds": round(time.perf_counter()-soak_start, 2)}), flush=True)
        finally:
            await backend.release_sequence(sequence)
        isolation = await generation_isolation(backend, model, template)
        chunked_isolation = await generation_isolation(
            backend, model, template,
            "\nDictionary task. " + "Select an allowed label using only the preceding context. " * 16
            + "Choose A or B. Label:")
        final = await backend.runtime_info()
        assert final["active_sequences"] == final["active_branches"] == 0
        assert final["fork_count"] == final["release_count"]
        summary = {"semantic_pass": sum(r["pass"] for r in results), "semantic_total": len(results),
                   "selection_median_ms": round(statistics.median(r["ms"] for r in results), 1),
                   "cycles": cycles, "prediction": prediction_report,
                   "isolation": isolation, "chunked_isolation": chunked_isolation,
                   "runtime": final}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if not all(r["pass"] for r in results):
            raise AssertionError("Semantic acceptance has failures")
        return summary
    finally:
        await backend.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=config.KV_ADAPTER_URL)
    parser.add_argument("--cycles", type=int, default=1000)
    args = parser.parse_args()
    if not args.url or not 1 <= args.cycles <= 10000:
        parser.error("A native URL and 1..10000 cycles are required")
    asyncio.run(run(args.url, args.cycles))


if __name__ == "__main__":
    main()
