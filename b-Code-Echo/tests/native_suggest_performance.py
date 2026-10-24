"""PERF-004: read-only candidate API timings against the running local model.

Measure repeated half-word prefixes and changing words, with/without history.
No conversation, learned state, model or debug setting is written.
"""
import argparse
import json
import math
import statistics
import time

import httpx

from echo_app import config, predict


def distribution(values):
    ordered = sorted(values)
    return {"p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--web", default="http://127.0.0.1:8040")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    with httpx.Client(timeout=60, trust_env=False) as client:
        def post(url, body):
            response = client.post(url, json=body)
            response.raise_for_status()
            return response.json()

        status = client.get(args.web + "/api/status").raise_for_status().json()
        model = status["model"]
        template = predict.template_of(model.split(":")[0])
        if not template or not status["kv"]["supported"]:
            raise RuntimeError("benchmark requires a supported native model")
        history = []
        for _ in range(12):
            history.extend([
                {"role": "user", "content": "I enjoy walking in the park and reading books. What do you like to do on weekends?"},
                {"role": "assistant", "content": "I like visiting quiet parks with friends and talking about books. We often bring some food and spend a few hours there. What is your favorite place?"},
            ])
        output = {"model": model, "repeats": args.repeats, "cases": {}}
        for name, messages, texts in [
            ("same_prefix", [], ["I want to m", "I want to ma", "I want to mak", "I want to make"]),
            ("changing_words", [], ["I ", "I want ", "I want to ", "I want to learn ", "I want to learn English "]),
            ("history_words", history, ["I ", "I would ", "I would like ", "I would like to ", "I would like to learn "]),
            ("history_edits", history, ["I would like to learn ", "I would like to go ", "I would like ", "I would ", "I "]),
        ]:
            timings, firsts = [], []
            for _ in range(args.repeats):
                # Formal A creation discards idle prediction KV in the new kernel.
                handle = post(config.KV_ADAPTER_URL + "/v1/kv/sequences", {"model": model, "text": "benchmark reset"})
                post(config.KV_ADAPTER_URL + f"/v1/kv/sequences/{handle['sequence_id']}/release", {"cache_epoch": handle["cache_epoch"]})
                for index, text in enumerate(texts):
                    started = time.perf_counter()
                    items = post(args.web + "/api/suggest", {"messages": messages, "text": text, "level": "cefr:A2", "candidates": 5})
                    elapsed = (time.perf_counter() - started) * 1000
                    assert isinstance(items, list) and len(items) <= 5, items
                    (firsts if index == 0 else timings).append(elapsed)
            output["cases"][name] = {"first": distribution(firsts), "following": distribution(timings)}
            print(json.dumps({name: output["cases"][name]}, ensure_ascii=False), flush=True)
        runtime = client.get(config.KV_ADAPTER_URL + "/v1/kv/runtime").raise_for_status().json()
        assert runtime["active_sequences"] == runtime["active_branches"] == 0, runtime
        output["runtime"] = runtime
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
