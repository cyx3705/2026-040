"""EVAL-001: same model, same inputs; native shared-KV runtime vs ordinary model API.

Run once per mode; only one copy of the model fits in 12 GB VRAM.
  --mode native  needs the idle native adapter (Echo's default runtime).
  --mode api     needs ECHO_KV_ADAPTER_URL="" and the private Ollama engine
                 with the native adapter stopped. It measures Echo's shipped
                 HTTP compatibility path plus a free-form translation baseline.
Writes no conversations, learned words or settings: a temporary user DB is used.
Timings are observations on one machine, not assertions.
"""
import argparse
import asyncio
import json
import math
import re
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

import httpx

from echo_app import config, predict

SUGGEST = [  # same sequences as native_suggest_performance.py
    ("same_prefix", False, ["I want to m", "I want to ma", "I want to mak", "I want to make"]),
    ("changing_words", False, ["I ", "I want ", "I want to ", "I want to learn ", "I want to learn English "]),
    ("history_words", True, ["I ", "I would ", "I would like ", "I would like to ", "I would like to learn "]),
    ("history_edits", True, ["I would like to learn ", "I would like to go ", "I would like ", "I would ", "I "]),
]
# Every keystroke of one learner message after 12 dialogue turns.
_TYPED = "I would like to visit a quiet park with my friends."
SUGGEST.append(("history_keystrokes", True, [_TYPED[:i] for i in range(1, len(_TYPED) + 1)]))
_TOKENS = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[A-Za-z']+|[.,?!]", _TYPED)]


def typed_target(text):
    """Ground truth for hit@k: the word being typed, else the next word or mark; None at the end."""
    if not _TYPED.startswith(text):
        return None
    partial = predict._PARTIAL.search(text)
    if partial and not text[-1:].isspace():
        return next(word for start, end, word in _TOKENS if start <= partial.start() < end)
    return next((word for start, _, word in _TOKENS if start >= len(text)), None)


# Typical prompt-engineering alternative: ask the chat model what comes next.
PREDICT_PROMPT = ("The learner is typing their next message to you. Their unfinished text is:\n\"{text}\"\n"
                  "Predict the 5 words the learner is most likely to type next. If the text ends in the middle "
                  "of a word, predict completions of that word. Reply only with JSON.")
PREDICT_SCHEMA = {"type": "object", "properties": {"words": {"type": "array", "items": {"type": "string"},
                                                             "minItems": 5, "maxItems": 5}},
                  "required": ["words"]}
# Usability thresholds (ms): perceived as instantaneous; mean and slow-typist
# inter-key intervals (Dhakal et al., CHI 2018); flow of thought kept.
THRESHOLDS = (100, 239, 481, 1000)
HISTORY = [{"role": "user", "content": "I enjoy walking in the park and reading books. What do you like to do on weekends?"},
           {"role": "assistant", "content": "I like visiting quiet parks with friends and talking about books. We often bring some food and spend a few hours there. What is your favorite place?"}] * 12

SENSES = [  # same cases as native_acceptance.py; "0" means abstention expected
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

REVERSE = [  # learner text before / Chinese word / after, and acceptable English
    ("I want to eat an ", "苹果", " after lunch.", {"apple"}),
    ("", "你好", ", how are you today?", {"hi", "hello"}),
    ("I want to ", "学习", " English every day.", {"learn", "study"}),
    ("I need to go to the ", "银行", " to get some money.", {"bank"}),
    ("Can I have some ", "冰淇淋", "?", {"ice cream", "ice-cream"}),
]

CHAT = ("Describe a walk beside a river bank in five simple sentences. "
        "Start with The river bank. Then ask me one question.")


def dist(values):
    ordered = sorted(values)
    return {"n": len(values), "p50_ms": round(statistics.median(values), 1),
            "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 1)}


def norm(text):
    return text.strip().strip(".!?\"'“”`").lower()


def within(values):
    return {f"<= {limit} ms": round(sum(v <= limit for v in values) / len(values), 3) for limit in THRESHOLDS}


async def prompt_predict(client, model, system, history, text):
    """One chat request per input change, structured as five words."""
    response = await client.post(config.ENGINE_URL + "/api/chat", json={
        "model": model, "stream": False, "think": False, "keep_alive": config.KEEP_ALIVE,
        "format": PREDICT_SCHEMA, "options": {"temperature": 0, "num_predict": 64},
        "messages": [{"role": "system", "content": system}, *history,
                     {"role": "user", "content": PREDICT_PROMPT.format(text=text)}]})
    response.raise_for_status()
    try:
        words = json.loads(response.json()["message"]["content"]).get("words", [])
    except (ValueError, AttributeError):
        words = []
    return [w for w in words if isinstance(w, str)]


async def suggest_case(mode, server, backend, repeats, ollama=None):
    """Next-input prediction per input change; native one-pass vs API methods."""
    out = {}
    template, model = "chatml", server.current_model()
    system = config.system_prompt("cefr:A2")
    methods = ["native"] if mode == "native" else ["api_one_pass", "api_prompt"]
    for method in methods:
        out[method] = {}
        for name, with_history, texts in SUGGEST:
            history = HISTORY if with_history else []
            firsts, following, valid, hits, samples = [], [], [], [], []
            for repeat in range(repeats):
                if method == "native":  # creating a source clears the idle prediction KV
                    handle = await backend.create_sequence(model, "benchmark reset")
                    await backend.release_sequence(handle)
                for index, text in enumerate(texts):
                    started = time.perf_counter()
                    if method == "api_prompt":
                        words = await prompt_predict(ollama, model, system, history, text)
                    else:
                        items = await predict.suggest(
                            model, template, system, history, text, k=5,
                            complete=server.dictionary.complete, is_word=server.dictionary.is_word,
                            top_tokens=backend.top_tokens if method == "native" else None)
                        words = [item["word"] for item in items]
                    (following if index else firsts).append((time.perf_counter() - started) * 1000)
                    partial = predict._PARTIAL.search(text)
                    prefix = partial.group(0).lower() if partial and not text[-1:].isspace() else ""
                    # Usable candidate: one dictionary word (or punctuation) that fits a typed prefix.
                    valid.append(sum(1 for w in words[:5] if (w in predict.PUNCT or (
                        " " not in w.strip() and server.dictionary.is_word(w.strip())
                        and w.strip().lower().startswith(prefix)))))
                    target = typed_target(text) if name == "history_keystrokes" else None
                    if target is not None:
                        ranked = [w.strip().lower() for w in words[:5]]
                        hits.append((target.lower() in ranked[:1], target.lower() in ranked))
                    if repeat == 0:
                        samples.append({"text": text, "target": target, "words": words[:5]})
            out[method][name] = {"first": dist(firsts), "following": dist(following),
                                 "following_within": within(following), "following_raw": [round(v, 2) for v in following],
                                 "valid_candidates_mean": round(statistics.mean(valid), 2), "samples": samples}
            if hits:
                out[method][name].update(hit_at_1=round(statistics.mean(h[0] for h in hits), 3),
                                         hit_at_5=round(statistics.mean(h[1] for h in hits), 3),
                                         hit_inputs=len(hits))
            print(json.dumps({"suggest": method, "case": name,
                              **{k: v for k, v in out[method][name].items() if k not in ("following_raw", "samples")}}),
                  flush=True)
    return out


async def sense_case(mode, server, backend, ollama, rounds):
    """Per-word gloss selection after the reply prefix exists."""
    template, model = "chatml", server.current_model()
    rows = []
    for round_ in range(rounds):
        for name, context, prefix, word, expected in SENSES:
            messages = [{"role": "user", "content": context}] if context else []
            senses = server.dictionary.senses(word)
            row = {"case": name, "round": round_}
            if mode == "native":
                prompt = predict.assistant_prompt(template, config.system_prompt("cefr:A2"), messages)
                started = time.perf_counter()
                sequence = await backend.create_sequence(model, prompt + prefix)
                row["prefix_prefill_ms"] = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                branch = None
                try:
                    candidates = [{"label": s["text"], "pos": s["pos"], "meaning": s["text"]} for s in senses]
                    labels = list(dict.fromkeys(["0", *[c["label"] for c in candidates]]))
                    task = {"word": word, "candidates": candidates, "allowed_labels": labels,
                            "left_context": prefix[-160:], "lemma": server.dictionary.lemma(word),
                            "occurrence_id": "eval-target"}
                    suffix = await backend.tokenize(predict.sense_suffix(template, "", task), False)
                    for compact in (True, "grouped"):
                        if len(suffix) > 640:
                            suffix = await backend.tokenize(predict.sense_suffix(template, "", task, compact=compact), False)
                    branch = await backend.fork_at(sequence, sequence.prefix_tokens)
                    answer = await backend.decode_label(branch, suffix, labels, 16)
                    row["ms"] = (time.perf_counter() - started) * 1000
                finally:
                    if branch:
                        await backend.release(branch)
                    await backend.release_sequence(sequence)
                row.update(answer=answer, correct=answer in expected, in_dictionary=True)
            else:
                # Echo's shipped HTTP path: one structured-output request per word.
                labels = [server._label(i) for i in range(len(senses))]
                task = {"occurrence_id": "w0", "surface": word, "lemma": server.dictionary.lemma(word),
                        "assistant_prefix": prefix,
                        "candidates": [{"label": l, "pos": s["pos"], "meaning": s["text"]} for l, s in zip(labels, senses)],
                        "allowed_labels": ["0", *labels]}
                schema = {"type": "object", "properties": {"w0": {"type": "string", "enum": task["allowed_labels"]}},
                          "required": ["w0"], "additionalProperties": False}
                prompt = server._SENSE_PROMPT.format(tasks=json.dumps({"context": messages, "task": task}, ensure_ascii=False))
                started = time.perf_counter()
                raw = await server.engine.ask(model, prompt, max_tokens=32, format_schema=schema, temperature=0)
                row["ms"] = (time.perf_counter() - started) * 1000
                try:
                    label = json.loads(raw).get("w0")
                except (ValueError, AttributeError):
                    label = None
                answer = "0" if label == "0" else (senses[labels.index(label)]["text"] if label in labels else None)
                row.update(answer=answer, correct=answer in expected, in_dictionary=answer is not None)
                # Typical app: ask the model to translate the word in context, free text.
                started = time.perf_counter()
                free = await chat_once(ollama, model, [*messages, {"role": "user", "content":
                    f"In the English text \"{prefix}\", what does the last word \"{word}\" mean here? "
                    "Answer with a short Chinese translation only."}], 16)
                row["free_ms"] = (time.perf_counter() - started) * 1000
                row["free_answer"] = free
                row["free_correct"] = "0" not in expected and any(m in free for m in expected)
                row["free_in_dictionary"] = any(norm(free) == norm(s["text"]) for s in senses)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = {"selection": dist([r["ms"] for r in rows]),
               "correct": sum(r["correct"] for r in rows), "total": len(rows),
               "in_dictionary": sum(r["in_dictionary"] for r in rows)}
    if mode == "native":
        summary["prefix_prefill"] = dist([r["prefix_prefill_ms"] for r in rows])
    else:
        summary["free"] = {**dist([r["free_ms"] for r in rows]),
                           "correct": sum(r["free_correct"] for r in rows),
                           "in_dictionary": sum(r["free_in_dictionary"] for r in rows)}
    return {"summary": summary, "rows": rows}


async def chat_once(client, model, messages, tokens):
    response = await client.post(config.ENGINE_URL + "/api/chat", json={
        "model": model, "messages": messages, "stream": False, "think": False,
        "keep_alive": config.KEEP_ALIVE, "options": {"temperature": 0, "num_predict": tokens}})
    response.raise_for_status()
    return response.json()["message"]["content"].strip()


async def reverse_case(mode, server, backend, ollama, rounds):
    """Chinese typed in place -> English dictionary choice (INPUT-007)."""
    template, model = "chatml", server.current_model()
    rows = []
    for round_ in range(rounds):
        for before, chinese, after, expected in REVERSE:
            choices = server.dictionary.english_choices(chinese)
            prompt, labels = predict.translation_prompt(template, [], before, chinese, after, choices)
            allowed = ["0", *(" " + key for key in labels)]
            row = {"chinese": chinese, "round": round_, "choices": len(choices)}
            started = time.perf_counter()
            if mode == "native":
                sequence = await backend.create_sequence(model, prompt)
                branch = None
                try:
                    branch = await backend.fork_at(sequence, sequence.prefix_tokens)
                    suffix = await backend.tokenize("Label:", False)
                    picked = await backend.decode_label(branch, suffix, allowed, 1)
                finally:
                    if branch:
                        await backend.release(branch)
                    await backend.release_sequence(sequence)
            else:
                # Same prompt and label set through the generate API: one token,
                # best allowed label among the returned top-20 log-probabilities.
                first = await predict._generate(ollama, model, prompt + "Label:", num_predict=1, top_logprobs=20)
                tops = first.get("logprobs", [{}])[0].get("top_logprobs", [])
                picked = next((t["token"] for t in tops if t["token"] in allowed), None)
            row["ms"] = (time.perf_counter() - started) * 1000
            word = labels.get((picked or "").strip())
            row.update(answer=word, correct=norm(word or "") in expected)
            if mode == "api":
                started = time.perf_counter()
                free = await chat_once(ollama, model, [{"role": "user", "content":
                    f"A learner is writing: \"{before}{chinese}{after}\". Translate the Chinese word "
                    f"\"{chinese}\" into the English word or phrase that fits. Reply with the English only."}], 8)
                row.update(free_ms=(time.perf_counter() - started) * 1000, free_answer=free,
                           free_correct=norm(free) in expected,
                           free_in_dictionary=norm(free) in {norm(c) for c in choices})
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = {"selection": dist([r["ms"] for r in rows]), "correct": sum(r["correct"] for r in rows),
               "total": len(rows)}
    if mode == "api":
        summary["free"] = {**dist([r["free_ms"] for r in rows]), "correct": sum(r["free_correct"] for r in rows),
                           "in_dictionary": sum(r["free_in_dictionary"] for r in rows)}
    return {"summary": summary, "rows": rows}


async def reply_case(mode, server, repeats):
    """Partner reply plus glosses: when are glosses on screen relative to the text?"""
    reports = []
    for _ in range(repeats):
        messages = [{"role": "user", "content": CHAT}]
        arrivals, glosses, defaults, text_done = [], [], 0, None
        started = time.perf_counter()
        if mode == "native":
            response = await server.chat(server.ChatRequest(messages=[server.Message(**m) for m in messages],
                                                            level="cefr:A2", length=4))
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
                        glosses.append(now)
                    elif item.get("state") == "degraded":
                        defaults += 1
        else:
            # Best-effort API app: stream the reply and, with Ollama's second
            # slot, select each new ambiguous word concurrently (queue of 8).
            system = server.system_for("cefr:A2", 4, None, messages)
            queue, reply = asyncio.Queue(maxsize=8), ""

            async def worker():
                nonlocal defaults
                while (job := await queue.get()) is not None:
                    prefix, start = job
                    for item in await server.resolve_glosses(prefix, messages, None, native_offset=start):
                        if item.get("source") == "model":
                            glosses.append(time.perf_counter() - started)
                        elif item.get("state") == "degraded":
                            defaults += 1
            task, scheduled = asyncio.create_task(worker()), 0
            async for text in server.engine.chat(server.current_model(), system, messages):
                reply += text
                arrivals.append(time.perf_counter() - started)
                words = list(server._WORDS.finditer(reply))
                for number, match in enumerate(words[:-1]):
                    if number >= scheduled:
                        scheduled = number + 1
                        await enqueue(server, queue, reply, match)
            text_done = time.perf_counter() - started
            for number, match in enumerate(server._WORDS.finditer(reply)):
                if number >= scheduled:
                    await enqueue(server, queue, reply, match)
            await queue.put(None)
            await task
        gaps = sorted((b - a) * 1000 for a, b in zip(arrivals, arrivals[1:]))
        report = {"chunks": len(arrivals), "first_ms": round(arrivals[0] * 1000, 1),
                  "gap_p50_ms": round(statistics.median(gaps), 1),
                  "gap_p95_ms": round(gaps[int((len(gaps) - 1) * .95)], 1),
                  "english_ms": round(text_done * 1000, 1),
                  "complete_ms": round((time.perf_counter() - started) * 1000, 1),
                  "model_glosses": len(glosses), "degraded": defaults,
                  "glosses_before_text_done": sum(t < text_done for t in glosses)}
        reports.append(report)
        print(json.dumps({"reply": report}), flush=True)
    keys = ("first_ms", "gap_p50_ms", "gap_p95_ms", "english_ms", "complete_ms")
    return {"median": {k: round(statistics.median(r[k] for r in reports), 1) for k in keys}, "runs": reports}


async def enqueue(server, queue, reply, match):
    """Queue only words that need model selection, as the native path does."""
    defaults = await server.resolve_glosses(reply[:match.end()], [], None, native_offset=match.start(),
                                            default_only=True)
    if any(item["state"] == "degraded" for item in defaults):
        try:
            queue.put_nowait((reply[:match.end()], match.start()))
        except asyncio.QueueFull:
            pass


async def run(args):
    with sqlite3.connect(f"{config.USER_DB.as_uri()}?mode=ro", uri=True) as user:
        learned = [row[0] for row in user.execute("SELECT lemma FROM learned")]
    with tempfile.TemporaryDirectory() as directory:
        original = config.USER_DB
        config.USER_DB = Path(directory) / "eval.db"
        try:
            from echo_app import server
        finally:
            config.USER_DB = original
        server.store.set_setting("model", "qwen3:8b")
        server.engine.parallel = 2
        for lemma in learned:
            server.dictionary.set_learned(lemma, True)
        backend = server.engine.kv
        ollama = httpx.AsyncClient(timeout=120, trust_env=False)
        try:
            if args.mode == "native":
                info = await backend.runtime_info()
                assert info["kernel"] == "cpp" and info["model"] == "qwen3:8b", info
                assert info["active_sequences"] == info["active_branches"] == 0, "Echo must be idle"
            else:
                assert config.KV_ADAPTER_URL is None, "set ECHO_KV_ADAPTER_URL to an empty string"
                # Same slot count as the user's saved setting; stopped again below.
                await asyncio.to_thread(server.engine.start)
                await chat_once(ollama, "qwen3:8b", [{"role": "user", "content": "Hi"}], 1)  # load + warm
            result = {"mode": args.mode, "model": "qwen3:8b", "learned_lemmas": len(learned),
                      "date": time.strftime("%Y-%m-%d %H:%M")}
            result["suggest"] = await suggest_case(args.mode, server, backend, args.repeats, ollama)
            if not args.predict_only:
                result["sense"] = await sense_case(args.mode, server, backend, ollama, args.rounds)
                result["reverse"] = await reverse_case(args.mode, server, backend, ollama, args.rounds)
                result["reply"] = await reply_case(args.mode, server, args.replies)
            if args.mode == "native":
                final = await backend.runtime_info()
                assert final["active_sequences"] == final["active_branches"] == 0
                assert final["fork_count"] == final["release_count"]
                result["runtime"] = final
        finally:
            server.engine.stop()
            await ollama.aclose()
            await backend.aclose()
            server.dictionary._user.close()
            server.store._con.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: (v["summary"] if isinstance(v, dict) and "summary" in v else v)
                      for k, v in result.items() if k in ("sense", "reverse")}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("native", "api"), required=True)
    parser.add_argument("--repeats", type=int, default=5, help="suggestion sequence repeats")
    parser.add_argument("--rounds", type=int, default=3, help="passes over sense/reverse cases")
    parser.add_argument("--replies", type=int, default=3, help="partner replies with glosses")
    parser.add_argument("--predict-only", action="store_true", help="run only next-input prediction")
    parser.add_argument("--out", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
