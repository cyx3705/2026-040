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
WORD_END = [" ", "\n", ",", ".", "?", "!", ";", ":", '"', "<|im_end|>", "<turn|>", "<end_of_turn>"]
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


def template_of(family: str) -> str | None:
    """模型族 → 对话模板。续写 user 回合要自己拼 prompt，所以只支持认得模板的模型族。"""
    if family.startswith("qwen"):
        return "chatml"
    if family.startswith("gemma4"):
        return "gemma4"
    if family.startswith("gemma"):
        return "gemma"
    return None


def render(template: str, system: str, messages: list[dict], user_text: str) -> str:
    """按模型自己的聊天模板拼 prompt，结尾停在 user 回合里，不闭合。

    历史里的 assistant 回合不带思考块，和 /api/chat 设 think:false 时模型看到的一致。
    """
    if template == "chatml":  # qwen
        turn = lambda role, text: f"<|im_start|>{role}\n{text}<|im_end|>\n"
        head, open_user = turn("system", system), "<|im_start|>user\n"
    elif template == "gemma4":
        turn = lambda role, text: f"<|turn>{'model' if role == 'assistant' else role}\n{text}<turn|>\n"
        # gemma4 每个回合开头都要先出一个思考块（关了思考也是个空块）。不替它写好，
        # 回合第一个词的候选几乎全被 <|channel> 占掉，剩下的是 thought、stone 这种碎片。
        head, open_user = turn("system", system), "<|turn>user\n<|channel>thought\n<channel|>"
    else:  # gemma 2/3 没有 system 角色：系统提示词并进第一个 user 回合
        turn = lambda role, text: f"<start_of_turn>{'model' if role == 'assistant' else role}\n{text}<end_of_turn>\n"
        head, open_user = "", "<start_of_turn>user\n"
        if messages:
            messages = [{**messages[0], "content": f"{system}\n\n{messages[0]['content']}"}, *messages[1:]]
        else:
            user_text = f"{system}\n\n{user_text}"
    return head + "".join(turn(m["role"], m["content"]) for m in messages) + open_user + user_text


async def _generate(client: httpx.AsyncClient, model: str, prompt: str, **options) -> dict:
    body = {
        "model": model,
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


async def suggest(
    model: str, template: str, system: str, messages: list[dict], text: str, k: int = 5, complete=None
) -> list[dict]:
    """text 以空格结尾（或为空）时预测下一个词；以字母结尾时补全正在打的词。

    补全时不能把半截词直接喂给模型：prompt 断在 token 中间，模型续得很差
    （"mak" 会续成 "makng"）。所以退回到这个词开头前做前向，只留以已打字母开头的候选；
    模型凑不满 5 个，再用 complete(prefix, n) 按词频从词典补。
    """
    m = _PARTIAL.search(text)
    partial = m.group(0) if m and not text[-1:].isspace() else ""
    before = (text[: len(text) - len(partial)] if partial else text).rstrip()
    at_start = not before
    prompt = render(template, system, messages, before)

    client = _shared_client()
    first = await _generate(client, model, prompt, num_predict=1, top_logprobs=TOP_N)
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
        if at_start and not partial and not body[0].isupper():
            continue  # 回合开头的词不带空格，小写的多是半截（sem → sembly）；句首词本该大写
        if partial and not (body.lower().startswith(partial.lower()) or partial.lower().startswith(body.lower())):
            continue
        picked.append((tok, lp, "word"))

    # 各候选并行补全到词边界（引擎开了多槽并行）
    async def expand(tok: str) -> str:
        res = await _generate(client, model, prompt + tok, num_predict=EXPAND_MAX, stop=WORD_END)
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
