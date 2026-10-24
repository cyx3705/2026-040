"""PERF-001/002: measure real A/B streaming with an isolated conversation DB.

Needs an idle, running native adapter. Does not alter user history or settings.
Timings are observations, not timing assertions; generated lengths can vary.
"""
import argparse
import asyncio
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from echo_app import config
from echo_app.kv import RemoteKVBackend


async def run(repeats):
    backend = RemoteKVBackend(config.KV_ADAPTER_URL, timeout=60)
    try:
        info = await backend.runtime_info()
    finally:
        await backend.aclose()
    if info["active_sequences"] or info["active_branches"]:
        raise RuntimeError("Native runtime busy; finish the current reply first")
    with sqlite3.connect(f"{config.USER_DB.as_uri()}?mode=ro", uri=True) as user:
        learned = [row[0] for row in user.execute("SELECT lemma FROM learned")]
    reports = []
    with tempfile.TemporaryDirectory() as directory:
        original = config.USER_DB
        config.USER_DB = Path(directory) / "performance.db"
        try:
            from echo_app import server
        finally:
            config.USER_DB = original
        server.store.set_setting("model", info["model"])
        server.engine.parallel = 2
        for lemma in learned:
            server.dictionary.set_learned(lemma, True)
        try:
            for _ in range(repeats):
                request = server.ChatRequest(messages=[server.Message(
                    role="user", content="Describe a walk beside a river bank in five simple sentences. "
                    "Start with The river bank. Then ask me one question.")], level="cefr:A2", length=4)
                response = await server.chat(request)
                arrivals, selections, defaults = [], [], 0
                text_done = None
                started = time.perf_counter()
                async for raw in response.body_iterator:
                    event = json.loads(raw.strip()[6:])
                    now = time.perf_counter() - started
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    if event.get("delta"):
                        arrivals.append(now)
                    if event.get("text_done"):
                        text_done = now
                    for item in event.get("gloss", []):
                        if item.get("source") == "model":
                            selections.append(now)
                        elif item.get("state") == "degraded":
                            defaults += 1
                gaps = sorted((b-a)*1000 for a, b in zip(arrivals, arrivals[1:]))
                report = {"model": info["model"], "chunks": len(arrivals),
                          "first_ms": round(arrivals[0]*1000, 1),
                          "gap_p50_ms": round(statistics.median(gaps), 1),
                          "gap_p95_ms": round(gaps[int((len(gaps)-1)*.95)], 1),
                          "english_ms": round(text_done*1000, 1),
                          "complete_ms": round((time.perf_counter()-started)*1000, 1),
                          "first_model_gloss_ms": round(selections[0]*1000, 1) if selections else None,
                          "model_glosses": len(selections), "degraded": defaults,
                          "model_glosses_before_text_done": sum(t < text_done for t in selections)}
                reports.append(report)
                print(json.dumps(report), flush=True)
            final = await server.engine.kv.runtime_info()
            assert final["active_sequences"] == final["active_branches"] == 0
            assert final["fork_count"] == final["release_count"]
        finally:
            await server.engine.kv.aclose()
            server.dictionary._user.close()
            server.store._con.close()
    summary = {"runs": len(reports), "median": {
        key: round(statistics.median(r[key] for r in reports), 1)
        for key in ("first_ms", "gap_p50_ms", "gap_p95_ms", "english_ms", "complete_ms")}}
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be 1..10")
    asyncio.run(run(args.repeats))
