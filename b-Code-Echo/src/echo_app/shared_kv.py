"""ECHO-KV-001: handles owned by the external model service, never by Echo.

The provider serializes operations with ordinary inference on one context.
Sequence leases are exclusive and always reclaimed, including cancellation.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from . import config, predict
from .kv import KVCapabilities, KVUnsupported, SequenceHandle


async def available(engine, model):
    shared = getattr(engine, "shared_kv", None)
    if isinstance(shared, SharedKV) and (await shared.capabilities(model)).supported:
        return shared
    return None


def label_suffix(template, instruction):
    if template == "chatml":
        return ("<|im_end|>\n<|im_start|>user\n" + instruction + "<|im_end|>\n"
                "<|im_start|>assistant\n<think>\n\n</think>\n\nLabel:")
    if template == "gemma4":
        return "<turn|>\n<|turn>user\n" + instruction + "<turn|>\n<|turn>model\n<|channel>thought\n<channel|>Label:"
    return "<end_of_turn>\n<start_of_turn>user\n" + instruction + "<end_of_turn>\n<start_of_turn>model\nLabel:"


class SharedKV:
    def __init__(self, engine):
        self.engine = engine
        self.lock = asyncio.Lock()
        self._stream_supported = False

    async def op(self, operation, **body):
        response = await self.engine.client.post(f"{config.INFERENCE_URL}/kv/op",
                                                json={"op": operation, **body})
        if response.status_code in (400, 404, 409, 501):
            try:
                message = response.json().get("error", {}).get("message", "shared_kv_unavailable")
            except (ValueError, AttributeError):
                message = "shared_kv_unavailable"
            raise KVUnsupported(message)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise KVUnsupported("invalid_shared_kv_response")
        return data

    async def capabilities(self, model):
        try:
            data = await self.op("capabilities", model=model)
            flags = [data.get(k) is True for k in ("supported", "shared_prefix", "fork_at", "label_constraint", "release")]
            if not all(flags):
                raise KVUnsupported(data.get('reason') or "external_service_no_sequence_fork")
            self._stream_supported = data.get("generation_streaming") is True
            return KVCapabilities(str(data.get("backend", "external-shared-kv")), *flags,
                                  provider_parallel_slots=data.get("provider_parallel_slots"))
        except Exception as exc:
            self._stream_supported = False
            return KVCapabilities.unsupported("external-http", str(exc))

    @staticmethod
    def fields(sequence):
        return {"sequence_id": sequence.sequence_id, "cache_epoch": sequence.cache_epoch}

    async def reclaim(self, pending):
        # An allocated handle may arrive after task cancellation. Retain that
        # response and reclaim it before releasing local sequence admission.
        data = await pending
        await self.op("release", sequence_id=data["sequence_id"], cache_epoch=data["cache_epoch"])

    @asynccontextmanager
    async def sequence(self, model, prompt):
        async with self.lock, self.engine._requests:
            pending = asyncio.create_task(self.op("create", model=model, text=prompt))
            try:
                data = await asyncio.shield(pending)
            except asyncio.CancelledError:
                await asyncio.shield(self.reclaim(pending))
                raise
            sequence = SequenceHandle(str(data["sequence_id"]), str(data["cache_epoch"]), int(data["prefix_tokens"]))
            try:
                yield sequence
            finally:
                await asyncio.shield(self.op("release", **self.fields(sequence)))

    async def tokenize(self, text):
        result = await self.op("tokenize", text=text, add_special=False)
        return result["tokens"]

    async def prefix(self, sequence, text):
        result = await self.op("prefix", **self.fields(sequence), text=text)
        return result["prefix_tokens"], result["remainder"]

    async def select(self, sequence, jobs):
        data = await self.op("select", **self.fields(sequence), jobs=jobs)
        return self._validate_results(data, jobs)

    @staticmethod
    def _validate_results(data, jobs):
        results = data.get("results")
        if not isinstance(results, list) or len(results) != len(jobs):
            raise KVUnsupported("invalid_shared_results")
        for result, job in zip(results, jobs):
            if result.get("label") not in job["allowed_labels"] or result.get("decision_steps") != 1:
                raise KVUnsupported("invalid_shared_label")
        return results

    async def select_text(self, sequence, prefix_text, suffix_text, allowed_labels):
        """One provider round trip for prefix matching, suffix tokenization and select.

        Older patched providers can fall back to the three primitive operations;
        the current service keeps all three steps inside one queue task.
        """
        job = {"prefix_tokens": 0, "suffix_tokens": [],
               "allowed_labels": allowed_labels, "budget": 1}
        try:
            data = await self.op("select_text", **self.fields(sequence),
                                 prefix_text=prefix_text, suffix_text=suffix_text,
                                 allowed_labels=allowed_labels)
            return self._validate_results(data, [job])
        except KVUnsupported as exc:
            if "unknown_echo_operation" not in str(exc):
                raise
            n, remainder = await self.prefix(sequence, prefix_text)
            tokens = await self.tokenize(remainder + suffix_text)
            job["prefix_tokens"], job["suffix_tokens"] = n, tokens
            return await self.select(sequence, [job])

    async def choose(self, model, prompt, labels):
        async with self.sequence(model, prompt) as sequence:
            # Labels follow an explicit completion cue; no output JSON decoding.
            suffix = await self.tokenize("Label:")
            results = await self.select(sequence, [{"prefix_tokens": sequence.prefix_tokens,
                "suffix_tokens": suffix, "allowed_labels": labels, "budget": 1}])
            return results[0]["label"].strip()

    async def chat(self, sequence, max_tokens=1024):
        if self._stream_supported:
            async with self.engine.client.stream("POST", f"{config.INFERENCE_URL}/kv/op",
                    json={"op": "stream", **self.fields(sequence), "max_tokens": max_tokens}) as response:
                response.raise_for_status()
                finished = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        if not finished:
                            raise KVUnsupported("shared_stream_missing_finish")
                        return
                    data = json.loads(payload)
                    if data.get("error"):
                        raise KVUnsupported(str(data["error"]))
                    if data.get("delta"):
                        yield data["delta"]
                    finished = finished or data.get("done") is True
                raise KVUnsupported("incomplete_shared_kv_stream")

        # Compatibility for providers without generation_streaming. Overlap
        # one A step with the consumer's work, but each token still requires
        # HTTP transport and queue admission; this cannot match local streaming.
        pending = None
        try:
            for _ in range(max_tokens):
                if pending is None:
                    pending = asyncio.create_task(self.op("next", **self.fields(sequence)))
                data = await pending
                pending = None
                if data.get("done"):
                    return
                pending = asyncio.create_task(self.op("next", **self.fields(sequence)))
                if data.get("delta"):
                    yield data["delta"]
            if pending is not None:
                await pending
                pending = None
            await self.op("finish", **self.fields(sequence))
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
