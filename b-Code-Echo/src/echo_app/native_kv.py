"""HTTP adapter for Echo's C++ inference kernel.

Only HTTP validation/serialization lives here. Model state, generation,
prediction, KV ownership, locking and constrained label decoding live in kernel/.
"""
from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import config
from .cpp_kernel import NativeKVRuntime
from .kv import BranchHandle, KVCapabilities, KVUnsupported, SequenceHandle


def find_model_blob(model: str) -> Path:
    name, _, tag = model.partition(":")
    manifest = config.MODELS_DIR / "manifests" / "registry.ollama.ai" / "library" / name / (tag or "latest")
    data = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    layer = next(x for x in data["layers"] if x.get("mediaType") == "application/vnd.ollama.image.model")
    return config.MODELS_DIR / "blobs" / layer["digest"].replace(":", "-")


class SequenceRequest(BaseModel):
    model: str
    text: str = Field(max_length=20000)
    # Server-side digest of model + page/video; never a client path.
    session: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SessionFiles:
    """KV-001: one state file per reader session; newest kept within a byte/file budget."""

    def __init__(self, directory: Path, max_bytes: int, max_files: int) -> None:
        self.directory, self.max_bytes, self.max_files = directory, max_bytes, max_files
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def path(self, digest: str) -> Path:
        return self.directory / f"{digest}.kv"

    def prune(self, keep: Path) -> None:
        now, total, files = time.time(), 0, []
        for item in self.directory.glob("*.kv*"):
            try:
                stat = item.stat()
            except OSError:
                continue
            if item.suffix == ".tmp":
                if now - stat.st_mtime > 3600:  # Left by a killed save.
                    item.unlink(missing_ok=True)
            else:
                files.append((stat.st_mtime, stat.st_size, item))
        for index, (_, size, item) in enumerate(sorted(files, key=lambda x: x[0], reverse=True)):
            total += size
            if item != keep and (index >= self.max_files or total > self.max_bytes):
                item.unlink(missing_ok=True)

    def flush_later(self, runtime: NativeKVRuntime, delay: float = 2.0) -> None:
        # Debounced idle write: a page closed or an adapter killed later loses nothing.
        def run():
            try:
                runtime.flush_session()
            except (KVUnsupported, RuntimeError):
                pass
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(delay, run)
            self._timer.daemon = True
            self._timer.start()


class ForkRequest(BaseModel):
    sequence_id: str
    prefix_tokens: int = Field(ge=0)
    cache_epoch: str


class DecodeRequest(BaseModel):
    suffix_tokens: list[int]
    allowed_labels: list[str] = Field(min_length=1, max_length=256)
    budget: int = Field(ge=1, le=32)
    cache_epoch: str


class ChatRequest(BaseModel):
    model: str
    prompt: str = Field(max_length=24000)
    max_tokens: int = Field(default=512, ge=1, le=2048)
    seed: int | None = None


class BatchJob(BaseModel):
    suffix_tokens: list[int] = Field(min_length=1, max_length=640)
    allowed_labels: list[str] = Field(min_length=1, max_length=64)


class BatchRequest(BaseModel):
    sequence_id: str
    cache_epoch: str
    jobs: list[BatchJob] = Field(min_length=1, max_length=2)


class TopTokensRequest(BaseModel):
    prompt: str = Field(max_length=24000)
    top_k: int = Field(default=20, ge=1, le=20)


def make_app(runtime: NativeKVRuntime, sessions: SessionFiles | None = None) -> FastAPI:
    app = FastAPI(title="Echo native KV adapter")

    @app.get("/v1/kv/capabilities")
    def capabilities(model: str = ""):
        if model and model != getattr(runtime, "model_name", ""):
            return KVCapabilities.unsupported("llama.cpp-native", "model_mismatch").__dict__
        return runtime.capabilities().__dict__

    @app.get("/v1/kv/runtime")
    def runtime_info():
        return {**runtime.runtime_info(), "reader_sessions": sessions is not None}

    @app.post("/v1/kv/chat")
    async def chat(req: ChatRequest):
        from fastapi.responses import StreamingResponse
        import json
        import asyncio

        if req.model != runtime.model_name:
            raise HTTPException(409, "model_mismatch")
        sequence = await asyncio.to_thread(runtime.create_sequence, req.prompt, sample=True, seed=req.seed)

        async def events():
            handed_off = False
            try:
                yield "data: " + json.dumps({"sequence": sequence.__dict__}) + "\n\n"
                iterator = runtime.generate(sequence, req.max_tokens, req.seed)
                sentinel = object()
                while True:
                    delta = await asyncio.to_thread(next, iterator, sentinel)
                    if delta is sentinel:
                        break
                    yield "data: " + json.dumps({"delta": delta}, ensure_ascii=False) + "\n\n"
                yield "data: {\"done\":true}\n\n"
                handed_off = True
            except Exception as exc:
                yield "data: " + json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n\n"
            finally:
                if not handed_off:
                    await asyncio.to_thread(runtime.clear_sequence, sequence)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/kv/top-tokens")
    def top_tokens(req: TopTokensRequest):
        try:
            return {"top_logprobs": runtime.top_tokens(req.prompt, req.top_k)}
        except (KVUnsupported, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/kv/prefix")
    def prefix(req: dict):
        try:
            sequence = SequenceHandle(str(req["sequence_id"]), str(req["cache_epoch"]), int(req["prefix_tokens"]))
            count, remainder = runtime.prefix_for_text(sequence, str(req["text"]))
            return {"prefix_tokens": count, "remainder": remainder}
        except (KeyError, TypeError, ValueError, KVUnsupported) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/kv/sequences")
    def create_sequence(req: SequenceRequest):
        try:
            if req.model != runtime.model_name:
                raise KVUnsupported("model_mismatch")
            if req.session and sessions:
                path = sessions.path(req.session)
                if path.exists():
                    os.utime(path)  # Recency for pruning includes read-only revisits.
                handle = runtime.create_session(req.text, str(path))
                sessions.prune(path)
                return handle.__dict__
            return runtime.create_sequence(req.text).__dict__
        except (KVUnsupported, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/kv/tokenize")
    def tokenize(req: dict):
        try:
            return {"tokens": runtime.tokenize(
                str(req.get("text", "")), bool(req.get("add_special", True))
            )}
        except (KVUnsupported, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/kv/input-batch")
    def input_batch(req: BatchRequest):
        try:
            sequence = SequenceHandle(req.sequence_id, req.cache_epoch, 0)
            return {"results": runtime.select_batch(sequence, [job.model_dump() for job in req.jobs])}
        except (KVUnsupported, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/kv/fork")
    def fork(req: ForkRequest):
        try:
            return runtime.fork_at(SequenceHandle(req.sequence_id, req.cache_epoch, req.prefix_tokens), req.prefix_tokens).__dict__
        except (KVUnsupported, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/kv/branches/{branch_id}/decode-label")
    def decode(branch_id: str, req: DecodeRequest):
        try:
            branch = BranchHandle(branch_id, "a0", req.cache_epoch)
            label, steps = runtime.select_label(branch, req.suffix_tokens, req.allowed_labels, req.budget)
            return {"label": label, "decision_steps": steps}
        except (KVUnsupported, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/kv/branches/{branch_id}/release")
    def release(branch_id: str, req: dict):
        runtime.release(BranchHandle(branch_id, "a0", str(req.get("cache_epoch", ""))))
        return {"released": True}

    @app.post("/v1/kv/sequences/{sequence_id}/release")
    def release_sequence(sequence_id: str, req: dict):
        runtime.clear_sequence(SequenceHandle(sequence_id, str(req.get("cache_epoch", "")), 0))
        if sessions:
            sessions.flush_later(runtime)
        return {"released": True}

    return app


def main(argv=None):
    raise SystemExit("EXT-001: Echo 仅连接外部推理服务，不再启动原生模型适配器。")


if __name__ == "__main__":
    main()
