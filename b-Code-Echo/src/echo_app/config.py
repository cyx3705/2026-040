"""Echo 的固定配置：数据目录、私有引擎端口、默认模型。"""

import os
from pathlib import Path

# 引擎和模型都放在 Echo 自己的数据目录，不碰本机已装的 Ollama。
DATA_DIR = Path(os.environ.get("ECHO_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "Echo")
ENGINE_DIR = DATA_DIR / "engine"
ENGINE_EXE = ENGINE_DIR / "ollama.exe"
MODELS_DIR = DATA_DIR / "models"
ENGINE_LOG = DATA_DIR / "engine.log"

# 私有实例的端口，和本机 Ollama 默认的 11434 错开。
ENGINE_HOST = "127.0.0.1"
ENGINE_PORT = int(os.environ.get("ECHO_ENGINE_PORT", "11540"))
ENGINE_URL = f"http://{ENGINE_HOST}:{ENGINE_PORT}"

WEB_PORT = int(os.environ.get("ECHO_WEB_PORT", "8040"))

MODEL = os.environ.get("ECHO_MODEL", "qwen3:8b")
KEEP_ALIVE = "30m"

SYSTEM_PROMPT = (
    "You are Echo, a patient English conversation partner for a Chinese learner "
    "whose English is not yet fluent. Always reply in simple, natural English, "
    "in two or three short sentences, and keep the conversation going with a question."
)
