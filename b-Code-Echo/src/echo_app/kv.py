"""A/B cache-fork contract used by Echo's native inference adapter.

Ollama's public HTTP API does not expose sequence handles.  This module keeps
the application contract independent from that compatibility transport: a
native adapter can share A's prefix, create one temporary B, decode a bounded
label, and release B without changing the chat or dictionary code.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
from typing import Any, Protocol, Callable, Awaitable

import httpx


class KVUnsupported(RuntimeError):
    """Raised when the selected runtime cannot provide an exact cache fork."""


@dataclass(frozen=True)
class KVCapabilities:
    backend: str
    supported: bool
    shared_prefix: bool
    fork_at: bool
    label_constraint: bool
    release: bool
    reason: str | None = None

    @classmethod
    def unsupported(cls, backend: str, reason: str) -> "KVCapabilities":
        return cls(backend, False, False, False, False, False, reason)


@dataclass(frozen=True)
class SequenceHandle:
    sequence_id: str
    cache_epoch: str
    prefix_tokens: int


@dataclass(frozen=True)
class BranchHandle:
    branch_id: str
    sequence_id: str
    cache_epoch: str


class KVBackend(Protocol):
    async def capabilities(self, model: str) -> KVCapabilities: ...

    async def create_sequence(self, model: str, text: str) -> SequenceHandle: ...

    async def tokenize(self, text: str, add_special: bool = True) -> list[int]: ...

    async def fork_at(
        self, sequence: SequenceHandle, prefix_tokens: int,
    ) -> BranchHandle: ...

    async def decode_label(
        self,
        branch: BranchHandle,
        suffix_tokens: list[int],
        allowed_labels: list[str],
        budget: int,
    ) -> str: ...

    async def release(self, branch: BranchHandle) -> None: ...

    async def release_sequence(self, sequence: SequenceHandle) -> None: ...


class RemoteKVBackend:
    """Adapter for the small native service Echo can run beside its web app.

    The service is intentionally explicit.  It must report real shared-prefix
    support; an Ollama `/api/chat` request cannot satisfy this protocol.
    """

    def __init__(self, base_url: str | None, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        # Loopback HTTP needs neither a proxy nor a CA store. Reuse the pool:
        # creating a TLS-capable client for each fork previously cost seconds.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def runtime_info(self) -> dict[str, Any]:
        if not self.base_url:
            raise KVUnsupported("adapter_not_configured")
        response = await self.client.get(f"{self.base_url}/v1/kv/runtime")
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise KVUnsupported("adapter_invalid_runtime_response")
        return value

    async def capabilities(self, model: str) -> KVCapabilities:
        if not self.base_url:
            return KVCapabilities.unsupported("ollama-http", "adapter_not_configured")
        try:
            response = await self.client.get(
                f"{self.base_url}/v1/kv/capabilities", params={"model": model}
            )
            response.raise_for_status()
            data = response.json()
            return KVCapabilities(
                backend=str(data.get("backend", "native")),
                supported=bool(data.get("supported")),
                shared_prefix=bool(data.get("shared_prefix")),
                fork_at=bool(data.get("fork_at")),
                label_constraint=bool(data.get("label_constraint")),
                release=bool(data.get("release")),
                reason=data.get("reason"),
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return KVCapabilities.unsupported("native-adapter", f"capability_probe_failed:{type(exc).__name__}")

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise KVUnsupported("adapter_not_configured")
        response = await self.client.post(f"{self.base_url}{path}", json=payload)
        if response.status_code in (404, 501):
            raise KVUnsupported("adapter_endpoint_unavailable")
        if response.status_code == 409:
            raise KVUnsupported(str(response.json().get("detail", "resource_limit")))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise KVUnsupported("adapter_invalid_response")
        return data

    async def create_sequence(self, model: str, text: str, session: str | None = None) -> SequenceHandle:
        # The native worker can finish prefill after the caller disconnects.
        # Retain the response/handle even on cancellation, then reclaim it.
        body = {"model": model, "text": text, **({"session": session} if session else {})}
        pending = asyncio.create_task(self._post("/v1/kv/sequences", body))
        try:
            data = await asyncio.shield(pending)
        except asyncio.CancelledError:
            async def reclaim():
                data = await pending
                handle = SequenceHandle(str(data["sequence_id"]), str(data["cache_epoch"]), int(data["prefix_tokens"]))
                await self.release_sequence(handle)
            await asyncio.shield(reclaim())
            raise
        try:
            return SequenceHandle(str(data["sequence_id"]), str(data["cache_epoch"]), int(data["prefix_tokens"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise KVUnsupported("adapter_invalid_sequence_response") from exc

    async def prefix_for_text(self, sequence: SequenceHandle, text: str) -> tuple[int, str]:
        data = await self._post("/v1/kv/prefix", {
            "sequence_id": sequence.sequence_id,
            "cache_epoch": sequence.cache_epoch,
            "prefix_tokens": sequence.prefix_tokens,
            "text": text,
        })
        try:
            return int(data["prefix_tokens"]), str(data.get("remainder", ""))
        except (KeyError, TypeError, ValueError) as exc:
            raise KVUnsupported("adapter_invalid_prefix_response") from exc

    async def top_tokens(self, prompt: str, top_k: int = 20) -> list[dict[str, Any]]:
        data = await self._post("/v1/kv/top-tokens", {"prompt": prompt, "top_k": top_k})
        items = data.get("top_logprobs")
        if not isinstance(items, list):
            raise KVUnsupported("adapter_invalid_top_tokens_response")
        return items

    async def chat(self, model: str, prompt: str, on_sequence: Callable[[SequenceHandle], None]) -> Any:
        """Stream A from the native runtime and retain its actual sequence for B."""
        if not self.base_url:
            raise KVUnsupported("adapter_not_configured")
        import json

        async with self.client.stream("POST", f"{self.base_url}/v1/kv/chat", json={
            "model": model, "prompt": prompt, "max_tokens": 1024,
        }, timeout=httpx.Timeout(300, connect=5)) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                if "error" in data:
                    raise KVUnsupported(f"native_generation_failed:{data['error']}")
                if "sequence" in data:
                    item = data["sequence"]
                    on_sequence(SequenceHandle(
                        str(item["sequence_id"]), str(item["cache_epoch"]), int(item["prefix_tokens"]),
                    ))
                if data.get("delta"):
                    yield str(data["delta"])
                if data.get("done"):
                    break

    async def tokenize(self, text: str, add_special: bool = True) -> list[int]:
        data = await self._post("/v1/kv/tokenize", {"text": text, "add_special": add_special})
        tokens = data.get("tokens")
        if not isinstance(tokens, list) or not all(isinstance(token, int) for token in tokens):
            raise KVUnsupported("adapter_invalid_token_response")
        return tokens

    async def fork_at(self, sequence: SequenceHandle, prefix_tokens: int) -> BranchHandle:
        data = await self._post(
            "/v1/kv/fork",
            {
                "sequence_id": sequence.sequence_id,
                "prefix_tokens": prefix_tokens,
                "cache_epoch": sequence.cache_epoch,
            },
        )
        try:
            return BranchHandle(
                branch_id=str(data["branch_id"]),
                sequence_id=sequence.sequence_id,
                cache_epoch=str(data["cache_epoch"]),
            )
        except (KeyError, TypeError) as exc:
            raise KVUnsupported("adapter_invalid_fork_response") from exc

    async def decode_label(
        self, branch: BranchHandle, suffix_tokens: list[int],
        allowed_labels: list[str], budget: int,
    ) -> str:
        data = await self._post(
            f"/v1/kv/branches/{branch.branch_id}/decode-label",
            {
                "suffix_tokens": suffix_tokens,
                "allowed_labels": allowed_labels,
                "budget": budget,
                "cache_epoch": branch.cache_epoch,
            },
        )
        label = data.get("label")
        if not isinstance(label, str) or label not in allowed_labels:
            raise KVUnsupported("adapter_invalid_label")
        return label

    async def release(self, branch: BranchHandle) -> None:
        if not self.base_url:
            return
        response = await self.client.post(
            f"{self.base_url}/v1/kv/branches/{branch.branch_id}/release",
            json={"cache_epoch": branch.cache_epoch},
        )
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()

    async def select_batch(self, sequence: SequenceHandle, jobs: list[dict]) -> list[dict]:
        data = await self._post("/v1/kv/input-batch", {
            "sequence_id": sequence.sequence_id, "cache_epoch": sequence.cache_epoch, "jobs": jobs,
        })
        results = data.get("results")
        if not isinstance(results, list) or len(results) != len(jobs):
            raise KVUnsupported("adapter_invalid_batch_response")
        for item, job in zip(results, jobs):
            if item.get("label") not in job["allowed_labels"] or item.get("decision_steps") != 1:
                raise KVUnsupported("adapter_invalid_label")
        return results

    async def release_sequence(self, sequence: SequenceHandle) -> None:
        if not self.base_url:
            return
        response = await self.client.post(
            f"{self.base_url}/v1/kv/sequences/{sequence.sequence_id}/release",
            json={"cache_epoch": sequence.cache_epoch},
        )
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()


class KVForkCoordinator:
    """Run one short-lived B and guarantee release on every exit path."""

    def __init__(self, backend: KVBackend, max_queue: int = 8) -> None:
        self.backend = backend
        self.max_queue = max_queue
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    async def select_label(
        self,
        sequence: SequenceHandle,
        prefix_tokens: int,
        suffix_tokens: list[int],
        allowed_labels: list[str],
        budget: int = 8,
    ) -> str:
        """Fork at A's prefix, decode one constrained label, and release B."""
        if self._active:
            raise KVUnsupported("branch_busy")
        self._active = True
        branch: BranchHandle | None = None
        try:
            branch = await self.backend.fork_at(sequence, prefix_tokens)
            return await self.backend.decode_label(branch, suffix_tokens, allowed_labels, budget)
        finally:
            if branch is not None:
                try:
                    await self.backend.release(branch)
                finally:
                    self._active = False
            else:
                self._active = False
