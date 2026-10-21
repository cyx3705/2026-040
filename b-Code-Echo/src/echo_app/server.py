"""echo-serve：启动私有引擎，提供对话页面和接口。"""

import json
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .engine import Engine

STATIC = Path(__file__).parent / "static"
engine = Engine()


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict:
    return engine.status()


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


def main() -> None:
    url = f"http://127.0.0.1:{config.WEB_PORT}"
    print(f"Echo 运行在 {url}")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=config.WEB_PORT, log_level="warning")
