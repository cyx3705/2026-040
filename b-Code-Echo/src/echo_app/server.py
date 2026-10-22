"""echo-serve：启动私有引擎，提供对话页面和接口。"""

import asyncio
import json
import re
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, levels, practice, predict, setup
from .dictionary import Dictionary
from .engine import Engine
from .store import Store
from .wordlists import parse_docx

STATIC = Path(__file__).parent / "static"
engine = Engine()
dictionary = Dictionary()
store = Store()
_switching = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if engine.installed:
        engine.start()
    yield
    engine.stop()


app = FastAPI(title="Echo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    level: str = levels.DEFAULT
    length: int = 0  # 回答长度：0 自动，1～5 从一句到长篇
    conversation_id: int | None = None
    practice: dict | None = None  # 练词对话：{list_id, label, words: [{word, meaning}]}


class LookupRequest(BaseModel):
    words: list[str]


class SuggestRequest(BaseModel):
    messages: list[Message]
    text: str
    level: str = levels.DEFAULT
    length: int = 0
    practice: dict | None = None


class LearnedRequest(BaseModel):
    lemma: str
    learned: bool


class ModelRequest(BaseModel):
    name: str


def current_model() -> str:
    return store.setting("model", config.MODEL)


_families: dict[str, str] = {}


def model_family(name: str) -> str:
    """记住每个模型的模型族：输入预测每轮都要用，不能每次都去问引擎（新建连接约 0.5 秒）。"""
    if name not in _families:
        _families.update({m["name"]: m["family"] for m in engine.models()})
    return _families.get(name, "")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict:
    return {
        **engine.status(current_model()),
        "dictionary": dictionary.installed,
        "levels": levels.catalog(),
        "lengths": levels.lengths(),
    }


# ---- 模型：私有实例里装好的，加上本机 Ollama 里能一键链接过来的 ----


@app.get("/api/models")
def models() -> dict:
    installed = engine.models() if engine.running() else []
    names = {m["name"] for m in installed}
    items = [{**m, "installed": True} for m in installed]
    items += [{**m, "installed": False} for m in setup.local_models() if m["name"] not in names]
    for m in items:
        m["supported"] = predict.template_of(m["family"]) is not None
    return {"current": current_model(), "models": sorted(items, key=lambda m: m["name"])}


@app.post("/api/model")
async def set_model(req: ModelRequest) -> dict:
    """换模型：没装的先从本机 Ollama 链接过来，旧模型让出显存，新模型先加载好再返回。"""
    async with _switching:
        old = current_model()
        names = {m["name"] for m in engine.models()}
        if req.name not in names and not await asyncio.to_thread(setup.import_local_model, req.name):
            raise HTTPException(404, f"找不到模型 {req.name}")
        _families.clear()
        if predict.template_of(model_family(req.name)) is None:
            raise HTTPException(400, f"{req.name} 的对话模板 Echo 还不认得，没法做输入预测")
        if old != req.name:
            await engine.unload(old)
        await engine.load(req.name)
        store.set_setting("model", req.name)
    return {"current": req.name}


@app.post("/api/lookup")
def lookup(req: LookupRequest) -> dict:
    return dictionary.lookup_many(req.words[:2000])


@app.post("/api/suggest")
async def suggest(req: SuggestRequest) -> list[dict]:
    """用户正在打的这句话的下一个词：5 个整词候选，各带短释义。"""
    model = current_model()
    template = predict.template_of(model_family(model)) or "chatml"
    items = await predict.suggest(
        model, template, system_for(req.level, req.length, req.practice, [m.model_dump() for m in req.messages]),
        [m.model_dump() for m in req.messages], req.text, complete=dictionary.complete,
    )
    for item in items:
        if item["kind"] == "punct":
            item["gloss"] = PUNCT_NAMES.get(item["word"], "")
            continue
        entry = dictionary.lookup(item["word"])
        item["gloss"] = entry["gloss"] if entry else ""
    return items


PUNCT_NAMES = {".": "句号", ",": "逗号", "?": "问号", "!": "感叹号"}


@app.get("/api/learned")
def learned() -> list[dict]:
    return dictionary.learned()


@app.post("/api/learned")
def set_learned(req: LearnedRequest) -> dict:
    dictionary.set_learned(req.lemma.lower(), req.learned)
    return {"ok": True}


def system_for(level: str, length: int, plan: dict | None, messages: list[dict]) -> str:
    """难度和长度要求，加上练词要求（还没用上的词排在前面）。"""
    used = practice.used_words(plan, messages, dictionary.lookup)
    return config.system_prompt(level, length) + practice.prompt(plan, used)


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """流式回复；说完才存档（没成的一轮不进历史），done 事件带回对话编号。

    练词对话可以没有消息就开始：AI 先开口（发给模型的那句开场指令不存档）。
    """
    messages = [m.model_dump() for m in req.messages]
    plan = req.practice if practice.words_of(req.practice) else None
    prompt = messages or ([{"role": "user", "content": practice.OPENING}] if plan else [])

    async def events():
        reply = ""
        try:
            system = system_for(req.level, req.length, plan, messages)
            async for text in engine.chat(current_model(), system, prompt):
                reply += text
                yield sse({"delta": text})
            conv_id = store.save(
                req.conversation_id, [*messages, {"role": "assistant", "content": reply.strip()}],
                levels.normalize(req.level), plan, f"练词 · {plan.get('label') or '单词表'}" if plan else None,
            )
            used = sorted(practice.used_words(plan, messages, dictionary.lookup))
            if plan and plan.get("list_id") and messages:
                # 这一句新用上的词才计数：和去掉这句之前比
                before = practice.used_words(plan, messages[:-1], dictionary.lookup)
                store.count_used(plan["list_id"], [w for w in used if w not in before])
            yield sse({"done": True, "conversation_id": conv_id, "used": used})
        except Exception as exc:  # 引擎错误原样交给页面显示
            yield sse({"error": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")


# ---- 对话历史 ----


@app.get("/api/conversations")
def conversations(q: str = "") -> list[dict]:
    return store.list(q)


@app.get("/api/conversations/{conv_id}")
def conversation(conv_id: int) -> dict:
    conv = store.get(conv_id)
    if not conv:
        raise HTTPException(404, "没有这段对话")
    used = sorted(practice.used_words(conv["practice"], conv["messages"], dictionary.lookup))
    return {**conv, "level": levels.normalize(conv["level"]), "used": used}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int) -> dict:
    store.delete(conv_id)
    return {"ok": True}


# ---- 单词表：从 Word 导入，挑词练 ----


@app.get("/api/wordlists")
def wordlists() -> list[dict]:
    return store.wordlists()


@app.post("/api/wordlists")
async def import_wordlist(request: Request) -> dict:
    """页面把 .docx 原样发过来（不走表单上传，省掉 python-multipart 依赖），文件名放在头里。"""
    name = unquote(request.headers.get("x-filename", "单词表")).rsplit(".", 1)[0] or "单词表"
    try:
        items = parse_docx(await request.body())
    except Exception:  # 不是 .docx（老的 .doc、改了扩展名的文件）
        raise HTTPException(400, "读不了这个文件：只支持 Word 的 .docx 格式，老的 .doc 请先在 Word 里另存为 .docx")
    if not items:
        raise HTTPException(400, "这个文档里没找到单词：要每段一个词，或者表格里一行一个词")
    list_id = store.add_wordlist(name, items)
    return {"id": list_id, "name": name, "count": len(items)}


@app.get("/api/wordlists/{list_id}")
def wordlist(list_id: int) -> dict:
    wl = store.wordlist(list_id)
    if not wl:
        raise HTTPException(404, "没有这个单词表")
    for w in wl["words"]:  # 文档里没写释义的，用词典的短释义补上
        if not w["meaning"] and " " not in w["word"]:
            entry = dictionary.lookup(w["word"])
            w["meaning"] = entry["gloss"] if entry else ""
    return wl


@app.delete("/api/wordlists/{list_id}")
def delete_wordlist(list_id: int) -> dict:
    store.delete_wordlist(list_id)
    return {"ok": True}


TITLE_PROMPT = (
    "下面是一段英语口语练习对话。给它起一个简短的中文标题，4 到 10 个字，概括聊的话题。"
    "只输出标题本身，不要引号、不要标点、不要解释。\n\n{transcript}"
)


@app.post("/api/conversations/{conv_id}/title")
async def name_conversation(conv_id: int) -> dict:
    """自动命名：用当前模型按前几轮内容起个中文标题。已经起过的不再改。"""
    conv = store.get(conv_id)
    if not conv:
        raise HTTPException(404, "没有这段对话")
    if conv["titled"]:
        return {"title": conv["title"]}
    transcript = "\n".join(
        f"{'Learner' if m['role'] == 'user' else 'Echo'}: {m['content']}" for m in conv["messages"][:4]
    )
    raw = await engine.ask(current_model(), TITLE_PROMPT.format(transcript=transcript))
    title = clean_title(raw)
    if title:
        store.set_title(conv_id, title)
    return {"title": title or conv["title"]}


def clean_title(raw: str) -> str:
    line = next((s for s in raw.strip().splitlines() if s.strip()), "")
    line = re.sub(r"^(标题|Title)\s*[:：]\s*", "", line.strip(), flags=re.I)
    line = line.strip(" \t\"'“”‘’《》「」*#。.!！")
    return line[:16]


def already_running(url: str) -> bool:
    try:
        return httpx.get(f"{url}/api/status", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def main() -> None:
    url = f"http://127.0.0.1:{config.WEB_PORT}"
    open_browser = "--no-browser" not in sys.argv
    # 再点一次快捷方式时 Echo 已经在跑：只打开页面，不再抢端口
    if already_running(url):
        print(f"Echo 已在 {url} 运行")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"Echo 运行在 {url}（关掉本窗口即退出 Echo）")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=config.WEB_PORT, log_level="warning")
