"""Echo 私有推理引擎：自带的 Ollama 程序，跑在 Echo 自己的端口和模型目录上。"""

import json
import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator

import httpx

from . import config
from .kv import BranchHandle, KVCapabilities, RemoteKVBackend, SequenceHandle


class Engine:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self.parallel = config.ENGINE_PARALLEL
        self.kv = RemoteKVBackend(config.KV_ADAPTER_URL, config.KV_ADAPTER_TIMEOUT)
        self._kv_proc: subprocess.Popen | None = None

    @property
    def installed(self) -> bool:
        return config.ENGINE_EXE.is_file()

    def running(self) -> bool:
        try:
            return httpx.get(f"{config.ENGINE_URL}/api/version", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def start(self, wait: float = 30) -> None:
        """启动私有实例；端口上已有 Echo 引擎就直接复用。"""
        if self.running():
            return
        if not self.installed:
            raise RuntimeError("引擎未安装，先运行 echo-setup")
        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            OLLAMA_HOST=f"{config.ENGINE_HOST}:{config.ENGINE_PORT}",
            OLLAMA_MODELS=str(config.MODELS_DIR),
            OLLAMA_KEEP_ALIVE=config.KEEP_ALIVE,
            OLLAMA_NUM_PARALLEL=str(self.parallel),
        )
        with open(config.ENGINE_LOG, "ab") as log:
            self._proc = subprocess.Popen(
                [str(config.ENGINE_EXE), "serve"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"引擎启动即退出，见 {config.ENGINE_LOG}")
            if self.running():
                return
            time.sleep(0.3)
        raise RuntimeError(f"引擎 {wait:.0f} 秒内没有就绪，见 {config.ENGINE_LOG}")

    def stop(self) -> None:
        """只停自己启动的进程；复用来的实例不动。"""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    async def set_parallel(self, parallel: int, model: str) -> None:
        """槽数是 Ollama 进程配置：只重启自己管理的实例，失败则恢复原配置。"""
        if parallel not in (1, 2):
            raise ValueError("并发数只支持 1 或 2")
        if parallel == self.parallel:
            return
        if config.KV_ADAPTER_URL:
            runtime = await self.kv.runtime_info()
            if runtime.get("shared_chat") and runtime.get("model") == model:
                # The native KV pool always has A/B sequence IDs, not two
                # models. 1 delays B until A ends; 2 interleaves B prefills.
                # Never load Ollama's copy of the model alongside native A/B.
                self.parallel = parallel
                return
        if self.running() and (self._proc is None or self._proc.poll() is not None):
            raise RuntimeError("当前引擎由其他进程管理，请关闭 Echo 和私有引擎后重新启动")
        previous = self.parallel
        await asyncio.to_thread(self.stop)
        self.parallel = parallel
        try:
            await asyncio.to_thread(self.start)
            await self.load(model)
        except Exception as exc:
            await asyncio.to_thread(self.stop)
            self.parallel = previous
            try:
                await asyncio.to_thread(self.start)
                await self.load(model)
            except Exception as rollback:
                raise RuntimeError(f"切换并发失败：{exc}；恢复原配置也失败：{rollback}") from exc
            raise RuntimeError(f"切换并发失败，已恢复为 {previous}：{exc}") from exc

    def models(self) -> list[dict]:
        """私有实例里装好的模型：名字、大小、模型族（选对话模板要用）。"""
        resp = httpx.get(f"{config.ENGINE_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        return [
            {"name": m["name"], "size": m.get("size", 0), "family": m.get("details", {}).get("family", "")}
            for m in resp.json().get("models", [])
        ]

    def status(self, model: str) -> dict:
        if not self.installed:
            return {"state": "missing", "detail": f"引擎未安装（找不到 {config.ENGINE_EXE}），先运行 echo-setup"}
        if not self.running():
            return {"state": "stopped", "detail": "引擎没有在运行"}
        if model not in {m["name"] for m in self.models()}:
            return {"state": "no-model", "detail": f"缺少模型 {model}，先运行 echo-setup"}
        return {"state": "ready", "detail": "本地引擎就绪", "model": model}

    async def load(self, model: str) -> None:
        """空 prompt 只加载不生成：换模型后先把它请进显存。"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
            resp = await client.post(
                f"{config.ENGINE_URL}/api/generate", json={"model": model, "keep_alive": config.KEEP_ALIVE}
            )
            resp.raise_for_status()

    async def unload(self, model: str) -> None:
        """换走的模型立刻让出显存：12GB 显存放不下两份大模型。"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as client:
            await client.post(f"{config.ENGINE_URL}/api/generate", json={"model": model, "keep_alive": 0})

    async def start_kv(self, model: str) -> None:
        """Run A, B, and next-token inference in one llama.cpp context/model."""
        if not config.KV_ADAPTER_URL:
            return
        health = None
        try:
            health = await self.kv.runtime_info()
        except Exception:
            pass
        if health and health.get("shared_chat") and health.get("kernel") != "cpp":
            raise RuntimeError("原生适配器仍是旧版 Python 内核，请关闭 Echo 和旧适配器后重新启动")
        if health and health.get("shared_chat") and health.get("top_tokens") and health.get("model") == model:
            return
        if health and self._kv_proc is None:
            raise RuntimeError(
                f"11541 上的原生适配器加载的是 {health.get('model') or '未知模型'}；"
                f"请先关闭它再加载 {model}"
            )
        if self._kv_proc and self._kv_proc.poll() is None:
            await asyncio.to_thread(self._stop_kv)
        try:
            await self.unload(model)
        except Exception:
            pass
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(ECHO_KV_GPU_LAYERS=str(config.KV_ADAPTER_GPU_LAYERS))
        with open(config.KV_ADAPTER_LOG, "ab") as log:
            self._kv_proc = subprocess.Popen(
                [sys.executable, "-m", "echo_app.native_kv", "--model", model,
                 "--port", str(config.KV_ADAPTER_PORT), "--gpu-layers", str(config.KV_ADAPTER_GPU_LAYERS)],
                env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if self._kv_proc.poll() is not None:
                raise RuntimeError(f"KV 原生推理适配器启动失败，查看 {config.KV_ADAPTER_LOG}")
            try:
                health = await self.kv.runtime_info()
                if (health.get("shared_chat") and health.get("top_tokens")
                        and health.get("kernel") == "cpp" and health.get("model") == model):
                    return
            except Exception:
                await asyncio.sleep(0.5)
        await asyncio.to_thread(self._stop_kv)
        raise RuntimeError(f"KV 原生推理适配器 300 秒内未就绪，查看 {config.KV_ADAPTER_LOG}")

    def _stop_kv(self) -> None:
        if self._kv_proc and self._kv_proc.poll() is None:
            self._kv_proc.terminate()
            try:
                self._kv_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kv_proc.kill()
        self._kv_proc = None

    async def chat(
        self, model: str, system: str, messages: list[dict], *, native_prompt: str | None = None,
        on_sequence=None,
    ) -> AsyncIterator[str]:
        """流式生成 AI 回复，逐段产出文本。"""
        if native_prompt is not None:
            try:
                runtime = await self.kv.runtime_info()
            except Exception:
                runtime = {}
            if runtime.get("shared_chat") and runtime.get("model") != model:
                raise RuntimeError("原生推理模型与当前选择不一致，请重新选择模型")
            if runtime.get("shared_chat"):
                async for text in self.kv.chat(model, native_prompt, on_sequence or (lambda _: None)):
                    yield text
                return
            if config.KV_ADAPTER_URL:
                raise RuntimeError("原生推理尚未就绪，请查看 Echo 原生适配器日志")
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
            "think": False,
            "keep_alive": config.KEEP_ALIVE,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
            async with client.stream("POST", f"{config.ENGINE_URL}/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"引擎返回 {resp.status_code}：{detail}")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise RuntimeError(chunk["error"])
                    text = chunk.get("message", {}).get("content", "")
                    if text:
                        yield text

    async def ask(
        self, model: str, prompt: str, max_tokens: int = 40,
        format_schema: dict | str | None = None, temperature: float = 0.3,
    ) -> str:
        """一问一答、不流式：给对话起标题这类小活。"""
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "keep_alive": config.KEEP_ALIVE,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if format_schema is not None:
            body["format"] = format_schema
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
            resp = await client.post(f"{config.ENGINE_URL}/api/chat", json=body)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")

    async def kv_capabilities(self, model: str) -> KVCapabilities:
        """Report exact A/B cache-fork support for the selected model."""
        caps = await self.kv.capabilities(model)
        if caps.supported:
            try:
                runtime = await self.kv.runtime_info()
            except Exception:
                return KVCapabilities.unsupported(caps.backend, "native_chat_unavailable")
            if not runtime.get("shared_chat") or not runtime.get("top_tokens"):
                return KVCapabilities.unsupported(caps.backend, "native_chat_unavailable")
            if runtime.get("model") != model:
                return KVCapabilities.unsupported(caps.backend, "model_mismatch")
        return caps

    async def fork_at(self, sequence: SequenceHandle, prefix_tokens: int) -> BranchHandle:
        """Create one temporary B from an already computed A prefix."""
        return await self.kv.fork_at(sequence, prefix_tokens)

    async def kv_create_sequence(self, model: str, text: str, session: str | None = None) -> SequenceHandle:
        return await self.kv.create_sequence(model, text, session)

    async def kv_tokenize(self, text: str, add_special: bool = True) -> list[int]:
        return await self.kv.tokenize(text, add_special)

    async def kv_prefix_for_text(self, sequence: SequenceHandle, text: str) -> tuple[int, str]:
        return await self.kv.prefix_for_text(sequence, text)

    async def kv_top_tokens(self, prompt: str, top_k: int = 20) -> list[dict]:
        return await self.kv.top_tokens(prompt, top_k)

    async def kv_release_sequence(self, sequence: SequenceHandle) -> None:
        await self.kv.release_sequence(sequence)

    async def decode_label(
        self, branch: BranchHandle, suffix_tokens: list[int],
        allowed_labels: list[str], budget: int = 8,
    ) -> str:
        """Decode only a bounded dictionary label on B."""
        return await self.kv.decode_label(branch, suffix_tokens, allowed_labels, budget)

    async def release_branch(self, branch: BranchHandle) -> None:
        """Release B; the native adapter must make this operation idempotent."""
        await self.kv.release(branch)
