"""EXT-001: HTTP consumer; the provider owns model and process lifetimes."""
import asyncio
import json
from collections.abc import AsyncIterator
import httpx
from . import config
from .kv import KVCapabilities
from .shared_kv import SharedKV


class Engine:
    def __init__(self):
        self.parallel = config.ENGINE_PARALLEL
        self._requests = asyncio.Semaphore(self.parallel)
        self._client = None
        self._ranking = None
        self.shared_kv = SharedKV(self)

    @property
    def client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(config.INFERENCE_TIMEOUT, connect=5),
                headers=config.inference_headers(), trust_env=False)
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
        self._ranking = None

    def models(self):
        with httpx.Client(timeout=5, headers=config.inference_headers(), trust_env=False) as client:
            resp = client.get(f"{config.INFERENCE_URL}/models")
            resp.raise_for_status()
            result = []
            for item in resp.json().get("data", []):
                name = item["id"]
                family = item.get("meta", {}).get("general.architecture", "")
                if not family:
                    family = ("qwen" if "qwen" in name.lower() else
                              "gemma4" if "gemma4" in name.lower() else
                              "gemma" if "gemma" in name.lower() else "")
                result.append({"name": name, "size": item.get("meta", {}).get("size", 0), "family": family})
            return result

    def model_catalog(self):
        """Optional provider management; ordinary OpenAI services keep ID selection."""
        with httpx.Client(timeout=5, headers=config.inference_headers(), trust_env=False) as client:
            response = client.get(f"{config.INFERENCE_URL}/echo/models")
            if response.status_code not in (404, 405, 501):
                response.raise_for_status()
                data = response.json()
                if data.get('managed') is True and isinstance(data.get('models'), list):
                    return data
        return {'managed':False,'models':[{**m,'installed':True,'supported':True} for m in self.models()]}

    async def model_switch(self):
        response = await self.client.get(f"{config.INFERENCE_URL}/echo/model", timeout=5)
        if response.status_code in (404, 405, 501):
            return None
        response.raise_for_status()
        data = response.json()
        return data if data.get('managed') is True else None

    async def select_external_model(self, name):
        response = await self.client.post(f"{config.INFERENCE_URL}/echo/model",json={'name':name},timeout=10)
        if response.status_code >= 400:
            try:
                detail = response.json()['error']['message']
            except (ValueError, KeyError, TypeError):
                detail = '外部服务无法切换模型'
            raise RuntimeError(detail)
        result = response.json()
        job_id = result.get('job_id')
        transient_failures = 0
        async with asyncio.timeout(900):
            while result.get('state') == 'switching':
                await asyncio.sleep(1)
                try:
                    progress = await self.model_switch()
                except httpx.RequestError:
                    transient_failures += 1
                    if transient_failures >= 15:
                        raise
                    continue
                result = progress
                transient_failures = 0
                if not result or result.get('job_id') != job_id:
                    raise RuntimeError('外部模型切换任务已变化，请刷新模型列表')
        if result.get('state') != 'ready' or result.get('current') != name:
            raise RuntimeError(result.get('error','外部服务未完成模型切换'))
        return result

    def running(self):
        try:
            self.models()
            return True
        except (httpx.HTTPError, ValueError, KeyError):
            return False

    def status(self, model):
        try:
            names = {m["name"] for m in self.models()}
        except (httpx.HTTPError, ValueError, KeyError):
            return {"state": "stopped", "detail": "无法连接外部推理服务，请检查服务地址和授权"}
        if model not in names:
            return {"state": "no-model", "detail": f"外部服务未提供模型 {model}"}
        return {"state": "ready", "detail": "外部推理服务就绪", "model": model}

    async def set_parallel(self, parallel, model):
        if parallel not in (1, 2):
            raise ValueError("并发数只支持 1 或 2")
        self.parallel = parallel  # Echo HTTP requests only; never change provider slots.
        self._requests = asyncio.Semaphore(parallel)

    async def kv_capabilities(self, model):
        return await self.shared_kv.capabilities(model)

    async def kv_top_tokens(self, prompt, top_k=20, *, model=None):
        if not config.COMPLETION_URL:
            raise RuntimeError("未配置下一词概率接口（ECHO_COMPLETION_URL）")
        key = (model or config.MODEL, prompt, top_k)
        if self._ranking and self._ranking[0] == key:
            return self._ranking[1]
        async with self._requests:
            resp = await self.client.post(config.COMPLETION_URL, json={
                "model": key[0], "prompt": prompt, "n_predict": 1, "n_probs": top_k,
                "temperature": 0, "cache_prompt": True, "stream": False})
        resp.raise_for_status()
        rows = resp.json().get("completion_probabilities", [])
        if not rows or not isinstance(rows[0].get("top_logprobs"), list):
            raise RuntimeError("外部服务未返回下一词概率")
        tops = [{"token": t["token"], "logprob": t["logprob"]} for t in rows[0]["top_logprobs"]]
        self._ranking = (key, tops)
        return tops

    async def chat(self, model, system, messages, *, max_tokens=None) -> AsyncIterator[str]:
        body = {"model": model, "stream": True,
                "messages": ([{"role": "system", "content": system}] if system else []) + messages,
                "max_tokens": max_tokens or 1024, "chat_template_kwargs": {"enable_thinking": False}}
        if not body["messages"]:
            raise ValueError("外部对话请求缺少 messages")
        done = False
        async with self._requests:
            async with self.client.stream("POST", f"{config.INFERENCE_URL}/chat/completions", json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    if not data:
                        continue
                    event = json.loads(data)
                    if "error" in event:
                        raise RuntimeError("外部推理服务返回错误")
                    for choice in event.get("choices", []):
                        content = choice.get("delta", {}).get("content")
                        if content:
                            yield content
        if not done:
            raise RuntimeError("外部推理流意外结束，未收到完成标记")

    async def ask(self, model, prompt, max_tokens=80, format_schema=None, temperature=0.3):
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "stream": False, "temperature": temperature, "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False}}
        if format_schema is not None:
            body["response_format"] = ({"type": "json_object"} if format_schema == "json" else
                {"type": "json_schema", "json_schema": {
                    "name": "echo_selection", "strict": True, "schema": format_schema}})
        async with self._requests:
            resp = await self.client.post(f"{config.INFERENCE_URL}/chat/completions", json=body)
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("外部推理结果超过输出预算")
        return choice["message"].get("content") or ""
