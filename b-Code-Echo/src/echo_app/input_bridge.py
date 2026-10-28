"""INPUT-001: lifetime/ownership adapter for the local Windows input helper."""
import ctypes as C
import threading

from .kernel_build import ensure_built


class InputBridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._dll = None
        self._runtime = C.c_void_p()

    def _start(self):
        if self._dll is not None:
            return
        dll = C.CDLL(str(ensure_built(input_only=True)))
        signatures = {
            "echo_input_error": ([], C.c_char_p),
            "echo_input_create": ([C.POINTER(C.c_void_p)], C.c_int),
            "echo_input_destroy": ([C.c_void_p], None),
            "echo_input_acquire": ([C.c_void_p, C.c_wchar_p, C.POINTER(C.c_uint64)], C.c_int),
            "echo_input_confirm": ([C.c_void_p, C.c_uint64], C.c_int),
            "echo_input_bounds": ([C.c_void_p, C.c_uint64] + [C.c_double] * 7 + [C.c_uint32] * 2, C.c_int),
            "echo_input_refresh": ([C.c_void_p, C.c_uint64], C.c_uint),
            "echo_input_text": ([C.c_void_p, C.c_uint64, C.c_wchar_p, C.c_uint], C.c_int),
            "echo_input_release": ([C.c_void_p, C.c_uint64], C.c_int),
        }
        for name, (args, result) in signatures.items():
            method = getattr(dll, name)
            method.argtypes, method.restype = args, result
        if dll.echo_input_create(C.byref(self._runtime)):
            raise RuntimeError(dll.echo_input_error().decode())
        self._dll = dll

    def _status(self, owner, state):
        committed = ""
        if self._dll and owner:
            text = C.create_unicode_buffer(65537)
            if self._dll.echo_input_text(self._runtime, owner, text, len(text)):
                raise RuntimeError(self._dll.echo_input_error().decode())
            committed = text.value
        return {"owner": owner, "active": bool(state & 1), "ready": bool(state & 2),
                "chinese": bool(state & 4), "failed": bool(state & 8),
                "verified": bool(state & 16), "proxy": bool(state & 32), "committed": committed}

    def acquire(self, marker):
        with self._lock:
            self._start()
            owner = C.c_uint64()
            if self._dll.echo_input_acquire(self._runtime, marker, C.byref(owner)):
                raise RuntimeError(self._dll.echo_input_error().decode())
            return self._status(owner.value, self._dll.echo_input_refresh(self._runtime, owner.value))

    def refresh(self, owner):
        with self._lock:
            state = self._dll.echo_input_refresh(self._runtime, owner) if self._dll else 0
            return self._status(owner, state)

    def bounds(self, owner, bounds):
        with self._lock:
            if not self._dll:
                return
            values = [bounds[name] for name in ("left", "top", "width", "height", "viewport_width",
                                                "viewport_height", "font_size", "color", "background")]
            if self._dll.echo_input_bounds(self._runtime, owner, *values):
                raise RuntimeError(self._dll.echo_input_error().decode())

    def confirm(self, owner):
        with self._lock:
            if not self._dll or self._dll.echo_input_confirm(self._runtime, owner):
                raise RuntimeError(self._dll.echo_input_error().decode() if self._dll else "input_unavailable")
            return self._status(owner, self._dll.echo_input_refresh(self._runtime, owner))

    def release(self, owner):
        with self._lock:
            if self._dll and self._dll.echo_input_release(self._runtime, owner):
                raise RuntimeError(self._dll.echo_input_error().decode())

    def close(self):
        with self._lock:
            if self._dll:
                self._dll.echo_input_destroy(self._runtime)
                self._dll = None
                self._runtime = C.c_void_p()
