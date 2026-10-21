"""前向输入预测：续写 user 回合，给出用户下一个（或正在打的这个）词的 5 个整词候选。

一次前向拿下一 token 的前 20 候选，再把每个候选贪心补全到词边界。模型给的是 token：
" learn" 是一个整词的开头，"ing" 是接在前一个词后面的半截，"," 是标点，要分开处理。
"""

import asyncio
import math
import re

import httpx

from . import config

TOP_N = 20       # 第一步多取一些：去重、过滤后要凑满 5 个
EXPAND_MAX = 6   # 补全一个词最多再生成几个 token
WORD_END = [" ", "\n", ",", ".", "?", "!", ";", ":", '"', "<|im_end|>"]
PUNCT = {".", ",", "?", "!"}
_PARTIAL = re.compile(r"[A-Za-z']+$")
_LEAD = re.compile(r"^[A-Za-z']*")


_client: httpx.AsyncClient | None = None


def _shared_client() -> httpx.AsyncClient:
    """复用一个客户端：Windows 上每新建一个要加载一次证书库，约 0.5 秒。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5))
    return _client


def chatml(messages: list[dict], user_text: str) -> str:
    """按 qwen3 的 ChatML 模板拼 prompt，结尾停在 user 回合里，不闭合。

    历史里的 assistant 回合不带 think 块，和 /api/chat 设 think:false 时模型看到的一致。
    """
    parts = [f"<|im_start|>system\n{config.SYSTEM_PROMPT}<|im_end|>\n"]
    for m in messages:
        parts.append(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n")
    parts.append(f"<|im_start|>user\n{user_text}")
    return "".join(parts)


async def _generate(client: httpx.AsyncClient, prompt: str, **options) -> dict:
    body = {
        "model": config.MODEL,
        "prompt": prompt,
        "raw": True,
        "stream": False,
        "keep_alive": config.KEEP_ALIVE,
        "options": {"temperature": 0, **options},
    }
    if "top_logprobs" in options:
        body["logprobs"] = True
        body["top_logprobs"] = options.pop("top_logprobs")
        body["options"] = {"temperature": 0, **options}
    resp = await client.post(f"{config.ENGINE_URL}/api/generate", json=body)
    resp.raise_for_status()
    return resp.json()


async def suggest(messages: list[dict], text: str, k: int = 5, complete=None) -> list[dict]:
    """text 以空格结尾（或为空）时预测下一个词；以字母结尾时补全正在打的词。

    补全时不能把半截词直接喂给模型：prompt 断在 token 中间，模型续得很差
    （"mak" 会续成 "makng"）。所以退回到这个词开头前做前向，只留以已打字母开头的候选；
    模型凑不满 5 个，再用 complete(prefix, n) 按词频从词典补。
    """
    m = _PARTIAL.search(text)
    partial = m.group(0) if m and not text[-1:].isspace() else ""
    before = (text[: len(text) - len(partial)] if partial else text).rstrip()
    at_start = not before
    prompt = chatml(messages, before)

    client = _shared_client()
    first = await _generate(client, prompt, num_predict=1, top_logprobs=TOP_N)
    tops = first.get("logprobs", [{}])[0].get("top_logprobs", [])

    picked = []  # (token, logprob, kind)
    for t in tops:
        tok, lp = t["token"], t["logprob"]
        body = tok.lstrip(" ")
        if not partial and body in PUNCT and not at_start:
            picked.append((tok, lp, "punct"))
            continue
        if not body or not _LEAD.fullmatch(body) or not (tok.startswith(" ") or at_start):
            continue  # 换行、特殊符号、接在上个词后面的半截
        if partial and not (body.lower().startswith(partial.lower()) or partial.lower().startswith(body.lower())):
            continue
        picked.append((tok, lp, "word"))

    # 各候选并行补全到词边界（引擎开了多槽并行）
    async def expand(tok: str) -> str:
        res = await _generate(client, prompt + tok, num_predict=EXPAND_MAX, stop=WORD_END)
        return tok.lstrip(" ") + _LEAD.match(res.get("response", "")).group(0)

    # 多补几个备用：补全后可能撞车（make / makes 去重后不够 5 个）
    words = [p[0].strip() if p[2] == "punct" else None for p in picked[: k + 2]]
    todo = [i for i, w in enumerate(words) if w is None]
    for i, w in zip(todo, await asyncio.gather(*(expand(picked[i][0]) for i in todo))):
        words[i] = w

    result, seen = [], set()
    for (tok, lp, kind), word in zip(picked, words):
        key = word.lower()
        if partial and not key.startswith(partial.lower()):
            continue
        if word and key not in seen and len(result) < k:
            seen.add(key)
            result.append({"word": word, "prob": math.exp(lp), "kind": kind})
    if partial and len(result) < k and complete:
        for word in complete(partial, k * 2):
            if word.lower() not in seen and len(result) < k:
                seen.add(word.lower())
                # 保持用户已打字母的大小写：Sic → Sichuan
                result.append({"word": partial + word[len(partial):], "prob": 0.0, "kind": "dict"})
    return result
