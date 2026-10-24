"""echo-setup：把推理引擎和默认模型装进 Echo 自己的数据目录。

本机装过 Ollama 时，引擎和模型从本机硬链接过来（同盘不占额外空间，失败再复制）；
没装时，引擎从 GitHub 发布页分段续传下载，模型由私有引擎自己拉取。
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

from . import config
from .dictionary import build_db
from .engine import Engine

ENGINE_VERSION = "0.40.7"
RELEASES_API = "https://api.github.com/repos/ollama/ollama/releases/latest"
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
    extract_engine(zip_path, config.ENGINE_DIR)
    zip_path.unlink()


def extract_engine(zip_path: Path, destination: Path) -> None:
    """Extract only the bundled engine, omitting unused AMD binaries."""
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not any(Path(name).name.lower() == "ollama.exe" for name in names):
            raise RuntimeError("下载的 Ollama 压缩包缺少 ollama.exe")
        for name in names:
            parts = Path(name).parts
            if len(parts) > 2 and parts[0] == "lib" and parts[2] in SKIP_BACKENDS:
                continue
            zf.extract(name, destination)


def installed_engine_version() -> str | None:
    """Read the version embedded in Echo's private executable."""
    if not config.ENGINE_EXE.is_file():
        return None
    try:
        proc = subprocess.run(
            [str(config.ENGINE_EXE), "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(?:version is|version:)\s*v?([0-9]+(?:\.[0-9]+){1,3})", proc.stdout + proc.stderr, re.I)
    return match.group(1) if match else None


def latest_engine_version() -> str:
    """Resolve the latest official release without changing local files."""
    with httpx.Client(follow_redirects=True, timeout=20) as client:
        response = client.get(RELEASES_API, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        tag = response.json().get("tag_name", "")
    version = str(tag).removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
        raise RuntimeError(f"GitHub 返回了无法识别的 Ollama 版本：{tag!r}")
    return version


def upgrade_engine(version: str | None = None, check: bool = False) -> dict:
    """Download and atomically activate a newer private Ollama engine.

    The running private service must be stopped first.  The previous engine is
    renamed to a timestamped sibling so a failed activation remains recoverable.
    """
    target = version.removeprefix("v") if version else latest_engine_version()
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", target):
        raise ValueError(f"版本号格式不对：{target}")
    current = installed_engine_version()
    result = {"current": current, "target": target, "updated": False, "backup": None}
    if current == target:
        result["detail"] = "已经是目标版本"
        return result
    if check:
        result["detail"] = "可以升级"
        return result
    try:
        if httpx.get(f"{config.ENGINE_URL}/api/version", timeout=1).status_code == 200:
            raise RuntimeError("Echo 私有引擎仍在运行，请先关闭 echo-serve 后再升级")
    except httpx.HTTPError:
        pass

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.DATA_DIR, prefix=".engine-upgrade-") as folder:
        temp_root = Path(folder)
        archive = temp_root / "ollama-windows-amd64.zip"
        url = f"{RELEASES_API.rsplit('/releases/latest', 1)[0]}/releases/download/v{target}/ollama-windows-amd64.zip"
        download(url, archive)
        staged = temp_root / "engine"
        extract_engine(archive, staged)
        staged_exe = staged / "ollama.exe"
        if not staged_exe.is_file():
            raise RuntimeError("新引擎解压后缺少 ollama.exe")
        backup = config.DATA_DIR / f"engine.backup-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        old_engine = config.ENGINE_DIR
        try:
            if old_engine.exists():
                os.replace(old_engine, backup)
            os.replace(staged, old_engine)
        except Exception:
            if old_engine.exists() and not old_engine.joinpath("ollama.exe").exists():
                shutil.rmtree(old_engine, ignore_errors=True)
            if backup.exists() and not old_engine.exists():
                os.replace(backup, old_engine)
            raise
        result.update(updated=True, backup=str(backup) if backup.exists() else None,
                      current=installed_engine_version(), detail="升级完成")
    return result


def upgrade_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="升级 Echo 私有 Ollama 引擎")
    parser.add_argument("--version", help="目标版本，例如 0.40.8；默认使用 GitHub latest")
    parser.add_argument("--check", action="store_true", help="只检查版本，不下载或替换文件")
    args = parser.parse_args(argv)
    try:
        result = upgrade_engine(args.version, args.check)
    except Exception as exc:
        print(f"升级失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


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
