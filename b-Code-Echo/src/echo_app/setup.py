"""echo-setup：把推理引擎和默认模型装进 Echo 自己的数据目录。

本机装过 Ollama 时，引擎和模型从本机硬链接过来（同盘不占额外空间，失败再复制）；
没装时，引擎从 GitHub 发布页分段续传下载，模型由私有引擎自己拉取。
"""

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import httpx

from . import config
from .dictionary import build_db
from .engine import Engine

ENGINE_VERSION = "0.40.7"
ENGINE_ZIP_URL = (
    f"https://github.com/ollama/ollama/releases/download/v{ENGINE_VERSION}/ollama-windows-amd64.zip"
)
# 只带 NVIDIA 和通用后端；rocm（AMD）要约 1GB，用不上就不带。
SKIP_BACKENDS = {"rocm_v7_1"}

LOCAL_INSTALL = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama"
LOCAL_MODELS = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")


def link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def link_tree(src: Path, dst: Path) -> None:
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if rel.parts and rel.parts[0] in SKIP_BACKENDS:
            continue
        if path.is_file():
            link_or_copy(path, dst / rel)


def install_engine() -> None:
    if config.ENGINE_EXE.is_file():
        print(f"引擎已在 {config.ENGINE_DIR}")
        return
    if (LOCAL_INSTALL / "ollama.exe").is_file():
        print(f"从本机 Ollama 链接引擎：{LOCAL_INSTALL}")
        link_or_copy(LOCAL_INSTALL / "ollama.exe", config.ENGINE_EXE)
        link_tree(LOCAL_INSTALL / "lib" / "ollama", config.ENGINE_DIR / "lib" / "ollama")
        return
    zip_path = config.DATA_DIR / "ollama-windows-amd64.zip"
    download(ENGINE_ZIP_URL, zip_path)
    print("解压引擎…")
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            parts = Path(name).parts
            if len(parts) > 2 and parts[0] == "lib" and parts[2] in SKIP_BACKENDS:
                continue
            zf.extract(name, config.ENGINE_DIR)
    zip_path.unlink()


def download(url: str, dst: Path, chunk: int = 8 << 20, retries: int = 20) -> None:
    """分段续传下载：本机出网大传输会断，靠 Range 分段加重试，而不是加超时。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    # identity：不让服务器压缩，Range 的字节偏移才对得上文件本身
    with httpx.Client(
        follow_redirects=True, timeout=30, headers={"Accept-Encoding": "identity"}
    ) as client:
        total = int(client.head(url).headers["content-length"])
        failures = 0
        while (done := part.stat().st_size if part.exists() else 0) < total:
            end = min(done + chunk, total) - 1
            try:
                resp = client.get(url, headers={"Range": f"bytes={done}-{end}"})
                resp.raise_for_status()
                with open(part, "ab") as f:
                    f.write(resp.content)
                failures = 0
                print(f"\r下载 {done * 100 // total}%", end="", flush=True)
            except httpx.HTTPError as exc:
                failures += 1
                if failures > retries:
                    raise RuntimeError(f"下载反复失败：{exc}") from exc
    print()
    part.replace(dst)


def manifest_rel(model: str) -> Path:
    name, _, tag = model.partition(":")
    return Path("registry.ollama.ai") / "library" / name / (tag or "latest")


def local_models() -> list[dict]:
    """本机 Ollama 已下载的模型（只看官方库里的），可以一键链接进 Echo。"""
    root = LOCAL_MODELS / "manifests" / "registry.ollama.ai" / "library"
    found = []
    for manifest in sorted(root.glob("*/*")) if root.is_dir() else []:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            blob = LOCAL_MODELS / "blobs" / data["config"]["digest"].replace(":", "-")
            family = json.loads(blob.read_text(encoding="utf-8")).get("model_family", "")
        except (OSError, ValueError, KeyError):
            continue
        size = sum(layer.get("size", 0) for layer in data.get("layers", []))
        found.append({"name": f"{manifest.parent.name}:{manifest.name}", "size": size, "family": family})
    return found


def import_local_model(model: str) -> bool:
    """把本机 Ollama 已下载的模型链接进 Echo 的模型目录。"""
    manifest = LOCAL_MODELS / "manifests" / manifest_rel(model)
    if not manifest.is_file():
        return False
    print(f"从本机 Ollama 链接模型 {model}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    digests = [data["config"]["digest"], *(layer["digest"] for layer in data["layers"])]
    for digest in digests:
        blob = digest.replace(":", "-")
        link_or_copy(LOCAL_MODELS / "blobs" / blob, config.MODELS_DIR / "blobs" / blob)
        meta = LOCAL_MODELS / "metadata" / f"{blob}.json"
        if meta.is_file():
            link_or_copy(meta, config.MODELS_DIR / "metadata" / meta.name)
    link_or_copy(manifest, config.MODELS_DIR / "manifests" / manifest_rel(model))
    name, _, tag = model.partition(":")
    v2 = LOCAL_MODELS / "manifests-v2" / "ollama.com" / "library" / name / (tag or "latest")
    if v2.is_file():
        link_or_copy(v2, config.MODELS_DIR / "manifests-v2" / v2.relative_to(LOCAL_MODELS / "manifests-v2"))
    return True


def pull_model(model: str) -> None:
    print(f"由私有引擎拉取模型 {model}")
    with httpx.stream(
        "POST", f"{config.ENGINE_URL}/api/pull", json={"model": model}, timeout=None
    ) as resp:
        for line in resp.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if "error" in msg:
                raise RuntimeError(msg["error"])
            if msg.get("total"):
                print(f"\r{msg['status']} {msg.get('completed', 0) * 100 // msg['total']}%", end="", flush=True)
    print()


def install_dictionary() -> None:
    if config.DICT_DB.is_file():
        print(f"词典已在 {config.DICT_DB}")
        return
    csv_path = config.DICT_DB.with_name("ecdict.csv")
    if not csv_path.is_file():
        print("下载 ECDICT 词典（约 66MB）")
        download(config.DICT_CSV_URL, csv_path, chunk=4 << 20)
    print("建立词典索引…")
    rows = build_db(csv_path, config.DICT_DB)
    csv_path.unlink()
    print(f"词典就绪：{rows} 条")


def main() -> None:
    install_dictionary()
    install_engine()
    engine = Engine()
    if config.MODEL not in ({m["name"] for m in engine.models()} if engine.running() else set()):
        if not import_local_model(config.MODEL):
            engine.start()
            pull_model(config.MODEL)
    engine.start()
    try:
        status = engine.status(config.MODEL)
    finally:
        engine.stop()
    print(status["detail"])
    sys.exit(0 if status["state"] == "ready" else 1)
