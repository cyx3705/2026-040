"""Echo 私有推理引擎：自带的 Ollama 程序，跑在 Echo 自己的端口和模型目录上。"""

import json
import os
import subprocess
import time
from collections.abc import AsyncIterator

import httpx

from . import config


class Engine:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

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
            OLLAMA_NUM_PARALLEL=str(config.ENGINE_PARALLEL),
        )
        log = open(config.ENGINE_LOG, "ab")
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

    def models(self) -> list[str]:
        resp = httpx.get(f"{config.ENGINE_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    def status(self) -> dict:
        if not self.installed:
            return {"state": "missing", "detail": f"引擎未安装（找不到 {config.ENGINE_EXE}），先运行 echo-setup"}
        if not self.running():
            return {"state": "stopped", "detail": "引擎没有在运行"}
        if config.MODEL not in self.models():
            return {"state": "no-model", "detail": f"缺少模型 {config.MODEL}，先运行 echo-setup"}
        return {"state": "ready", "detail": "本地引擎就绪", "model": config.MODEL}

    async def chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式生成 AI 回复，逐段产出文本。"""
        body = {
            "model": config.MODEL,
            "messages": [{"role": "system", "content": config.SYSTEM_PROMPT}, *messages],
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
