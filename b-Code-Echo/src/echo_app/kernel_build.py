"""Build Echo's own C++ kernel into the external data directory, never libllama."""
import hashlib
import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from . import config

SOURCE = Path(__file__).with_name("kernel")


def build_directory(input_only=False) -> Path:
    digest = hashlib.sha256()
    for path in sorted(SOURCE.iterdir()):
        if path.suffix in {".cpp", ".h", ".ps1"} and (not input_only or path.name.startswith("input_") or path.suffix == ".ps1"):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return config.DATA_DIR / "native" / (("input-" if input_only else "") + digest.hexdigest()[:16])


def ensure_built(input_only=False) -> Path:
    if os.name != "nt":
        raise RuntimeError("Echo C++ kernel currently requires Windows x64")
    directory = build_directory(input_only)
    dll = directory / ("input_bridge.dll" if input_only else "echo_kernel.dll")
    marker = directory / "complete"
    if dll.is_file() and marker.is_file():
        return dll
    directory.mkdir(parents=True, exist_ok=True)
    # Serialise concurrent first launches; Windows byte-range locks are process scoped.
    import msvcrt
    with (directory / "build.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            if dll.is_file() and marker.is_file():
                return dll
            with tempfile.TemporaryFile() as log:
                result = subprocess.run([
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(SOURCE / "Build-Kernel.ps1"), "-OutputDirectory", str(directory),
                    *(["-InputOnly"] if input_only else []),
                ], stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    log.seek(0)
                    raise RuntimeError("C++ kernel build failed; run echo-build-input.\n" +
                                       log.read().decode("utf-8", "replace")[-12000:])
            marker.write_text("1", encoding="ascii")
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return dll


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Run input bridge tests without loading a model")
    args = parser.parse_args()
    dll = ensure_built(input_only=True)
    print(dll, flush=True)
    if args.test:
        subprocess.run([str(dll.with_name("input_tests.exe"))], check=True)


if __name__ == "__main__":
    main()
