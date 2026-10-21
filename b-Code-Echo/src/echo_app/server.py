"""echo-serve：启动私有引擎，提供对话页面和接口。"""

import json
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, predict
from .dictionary import Dictionary
from .engine import Engine

STATIC = Path(__file__).parent / "static"
engine = Engine()
dictionary = Dictionary()


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


class LookupRequest(BaseModel):
    words: list[str]


class SuggestRequest(BaseModel):
    messages: list[Message]
    text: str


class LearnedRequest(BaseModel):
    lemma: str
    learned: bool


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict:
    return {**engine.status(), "dictionary": dictionary.installed}


@app.post("/api/lookup")
def lookup(req: LookupRequest) -> dict:
    return dictionary.lookup_many(req.words[:500])


@app.post("/api/suggest")
async def suggest(req: SuggestRequest) -> list[dict]:
    """用户正在打的这句话的下一个词：5 个整词候选，各带短释义。"""
    items = await predict.suggest(
        [m.model_dump() for m in req.messages], req.text, complete=dictionary.complete
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


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    async def events():
        try:
            async for text in engine.chat([m.model_dump() for m in req.messages]):
                yield sse({"delta": text})
            yield sse({"done": True})
        except Exception as exc:  # 引擎错误原样交给页面显示
            yield sse({"error": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")


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
