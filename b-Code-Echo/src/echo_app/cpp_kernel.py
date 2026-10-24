"""Transport binding only: no model structs, sampling, ranking or KV state in Python."""
import ctypes as C
import json
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

from .kernel_build import ensure_built
from .kv import BranchHandle, KVCapabilities, KVUnsupported, SequenceHandle


def _library():
    lib = C.CDLL(str(ensure_built()))
    ptr, u64, integer, boolean = C.c_void_p, C.c_uint64, C.c_int, C.c_bool
    signatures = {
        "echo_abi": ([], integer), "echo_error": ([], C.c_char_p),
        "echo_free": ([ptr], None),
        "echo_open": ([C.c_wchar_p, C.c_char_p, integer, integer, C.POINTER(ptr)], integer),
        "echo_close": ([ptr], None),
        "echo_begin": ([ptr, C.c_char_p, C.c_uint32, boolean, C.POINTER(u64), C.POINTER(integer)], integer),
        "echo_step": ([ptr, u64, C.POINTER(boolean), C.POINTER(ptr)], integer),
        "echo_tokenize": ([ptr, C.c_char_p, boolean, C.POINTER(ptr)], integer),
        "echo_prefix": ([ptr, u64, C.c_char_p, C.POINTER(integer), C.POINTER(ptr)], integer),
        "echo_fork": ([ptr, u64, integer, C.POINTER(u64)], integer),
        "echo_select": ([ptr, u64, C.POINTER(C.c_int32), integer, C.POINTER(C.c_char_p),
                         integer, integer, C.POINTER(ptr), C.POINTER(integer)], integer),
        "echo_release": ([ptr, u64, boolean], integer),
        "echo_predict": ([ptr, C.c_char_p, integer, C.POINTER(ptr)], integer),
        "echo_select_batch": ([ptr, u64, integer, C.POINTER(C.c_int32), C.POINTER(integer),
                               C.POINTER(C.c_char_p), C.POINTER(integer), C.POINTER(ptr)], integer),
        "echo_info": ([ptr, C.POINTER(ptr)], integer),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
    if lib.echo_abi() != 1:
        raise RuntimeError("Echo C++ kernel ABI mismatch")
    return lib


def _utf8(text):
    if "\0" in text:
        raise KVUnsupported("invalid_text_nul")
    return text.encode("utf-8")


class NativeKVRuntime:
    """C++ owns model/context, A/B handles, mutex, cache, sampling and decoding.

    Python's condition protects only DLL lifetime against concurrent close().
    The condition is released during native calls, so A and B can interleave.
    """
    def __init__(self, model_path: Path, library_path: Path, context: int = 4096, gpu_layers: int = 0):
        self._dll = _library()
        self._lifetime = threading.Condition()
        self._calls = 0
        self._closing = False
        self._runtime = C.c_void_p()
        self.epoch = uuid.uuid4().hex
        self.context_size = context
        self._check(self._dll.echo_open(str(library_path), str(model_path).encode(), context,
                                        gpu_layers, C.byref(self._runtime)))

    def _check(self, rc):
        if rc:
            raise KVUnsupported(self._dll.echo_error().decode("utf-8", "replace"))

    def _invoke(self, name, *args):
        with self._lifetime:
            if self._closing or not self._runtime:
                raise KVUnsupported("runtime_closed")
            self._calls += 1
        try:
            # CDLL releases the GIL. Read thread-local error on this same thread.
            self._check(getattr(self._dll, name)(self._runtime, *args))
        finally:
            with self._lifetime:
                self._calls -= 1
                self._lifetime.notify_all()

    def _text(self, name, *args):
        out = C.c_void_p()
        try:
            self._invoke(name, *args, C.byref(out))
            return C.string_at(out).decode("utf-8", "replace")
        finally:
            if out:
                self._dll.echo_free(out)

    def _id(self, handle):
        if handle.cache_epoch != self.epoch:
            raise KVUnsupported("stale_cache_epoch")
        try:
            value = int(getattr(handle, "branch_id", None) or handle.sequence_id)
        except (ValueError, TypeError):
            raise KVUnsupported("invalid_handle") from None
        if not 0 < value < 2**64:
            raise KVUnsupported("invalid_handle")
        return value

    def close(self):
        with self._lifetime:
            self._closing = True
            while self._calls:
                self._lifetime.wait()
            if self._runtime:
                self._dll.echo_close(self._runtime)
                self._runtime = C.c_void_p()

    def capabilities(self):
        return KVCapabilities("llama.cpp-native", True, True, True, True, True)

    def runtime_info(self):
        return {**json.loads(self._text("echo_info")), "model": getattr(self, "model_name", ""),
                "shared_chat": True, "top_tokens": True, "backend": "llama.cpp-native", "kv_unified": True}

    def tokenize(self, text: str, add_special: bool = True) -> list[int]:
        return json.loads(self._text("echo_tokenize", _utf8(text), add_special))

    def create_sequence(self, text: str, *, sample=False, seed=None):
        id_, prefix = C.c_uint64(), C.c_int()
        self._invoke("echo_begin", _utf8(text), 0xFFFFFFFF if seed is None else seed & 0xFFFFFFFF,
                     sample, C.byref(id_), C.byref(prefix))
        return SequenceHandle(str(id_.value), self.epoch, prefix.value)

    def generate(self, sequence: SequenceHandle, max_tokens=512, seed=None) -> Iterator[str]:
        id_ = self._id(sequence)
        for _ in range(max_tokens):
            done = C.c_bool()
            text = self._text("echo_step", id_, C.byref(done))
            if done.value:
                break
            if text:
                yield text

    def top_tokens(self, prompt: str, limit=20):
        return json.loads(self._text("echo_predict", _utf8(prompt), limit))

    def prefix_for_text(self, sequence, text):
        prefix = C.c_int()
        remainder = self._text("echo_prefix", self._id(sequence), _utf8(text), C.byref(prefix))
        return prefix.value, remainder

    def fork_at(self, sequence, prefix_tokens):
        if not 0 <= prefix_tokens <= self.context_size:
            raise KVUnsupported("invalid_prefix_length")
        branch = C.c_uint64()
        self._invoke("echo_fork", self._id(sequence), prefix_tokens, C.byref(branch))
        return BranchHandle(str(branch.value), sequence.sequence_id, self.epoch)

    def select_label(self, branch, suffix_tokens, allowed_labels, budget):
        # Keep validation before array construction to bound external request allocations.
        if not 0 < len(suffix_tokens) <= 640 or not 0 < len(allowed_labels) <= 256:
            raise KVUnsupported("invalid_selection_input")
        if any(not 0 <= token < 2**31 for token in suffix_tokens):
            raise KVUnsupported("invalid_token_id")
        suffix = (C.c_int32 * len(suffix_tokens))(*suffix_tokens)
        labels = (C.c_char_p * len(allowed_labels))(*(_utf8(s) for s in allowed_labels))
        steps, out = C.c_int(), C.c_void_p()
        try:
            self._invoke("echo_select", self._id(branch), suffix, len(suffix_tokens), labels,
                         len(allowed_labels), budget, C.byref(out), C.byref(steps))
            return C.string_at(out).decode(), steps.value
        finally:
            if out:
                self._dll.echo_free(out)

    def decode_label(self, branch, suffix_tokens, allowed_labels, budget):
        return self.select_label(branch, suffix_tokens, allowed_labels, budget)[0]

    def select_batch(self, sequence, jobs):
        if not 1 <= len(jobs) <= 2:
            raise KVUnsupported("invalid_batch_size")
        for job in jobs:
            if (not 1 <= len(job["suffix_tokens"]) <= 640 or
                    not 1 <= len(job["allowed_labels"]) <= 64 or
                    any(not 0 <= t < 2**31 for t in job["suffix_tokens"])):
                raise KVUnsupported("invalid_selection_input")
        tokens = [t for job in jobs for t in job["suffix_tokens"]]
        labels = [_utf8(label) for job in jobs for label in job["allowed_labels"]]
        return json.loads(self._text("echo_select_batch", self._id(sequence), len(jobs),
            (C.c_int32 * len(tokens))(*tokens),
            (C.c_int * len(jobs))(*(len(job["suffix_tokens"]) for job in jobs)),
            (C.c_char_p * len(labels))(*labels),
            (C.c_int * len(jobs))(*(len(job["allowed_labels"]) for job in jobs))))

    def release(self, branch):
        if branch.cache_epoch == self.epoch:
            self._invoke("echo_release", self._id(branch), True)

    def clear_sequence(self, sequence):
        if sequence.cache_epoch == self.epoch:
            self._invoke("echo_release", self._id(sequence), False)
