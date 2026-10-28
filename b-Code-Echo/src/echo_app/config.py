"""Echo data and external inference configuration (EXT-001)."""

import os
from pathlib import Path

from .levels import describe

# Local dictionary, learning database and keyboard helper; no model weights are loaded.
DATA_DIR = Path(os.environ.get("ECHO_DATA_DIR") or Path.home() / ".echo")
ENGINE_DIR = DATA_DIR / "engine"
ENGINE_EXE = ENGINE_DIR / "ollama.exe"
MODELS_DIR = DATA_DIR / "models"
ENGINE_LOG = DATA_DIR / "engine.log"

# ECDICT（MIT）：setup 时下载 CSV，转成 SQLite 供离线查词。
DICT_CSV_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
DICT_DB = DATA_DIR / "dict" / "ecdict.db"
# 用户自己的数据（学会的词）。
USER_DB = DATA_DIR / "user.db"

# Retired native source utilities retain these paths for offline inspection.
# The app never reads model files, launches this adapter, or contacts old ports.
KV_ADAPTER_URL = None
KV_SESSION_DIR = DATA_DIR / "reader-kv"

WEB_PORT = int(os.environ.get("ECHO_WEB_PORT", "8040"))
WEB_HOST = os.environ.get("ECHO_WEB_HOST", "127.0.0.1").strip()
SHARE_KEY = os.environ.get("ECHO_SHARE_KEY", "").strip()
SHARE_KEY_FILE = DATA_DIR / "extension-key.txt"

# 默认模型；页面上换过之后记在 user.db 的设置里。
MODEL = os.environ.get("ECHO_MODEL", "qwen3.5:35b-a3b-q4_K_M")
# Max simultaneous inference HTTP requests made by Echo.
ENGINE_PARALLEL = 1


def system_prompt(level: str, length: int = 0) -> str:
    """难度和回答长度的要求见 levels.py。"""
    return ("You are Echo, a patient English conversation partner for a Chinese learner. "
            "Reply naturally in English. " + describe(level, length))

# OpenAI-compatible chat/model discovery; raw next-token probabilities use
# llama-server's /completion extension. An empty completion URL disables it.
INFERENCE_URL = os.environ.get("ECHO_INFERENCE_URL", "http://127.0.0.1:11435/v1").rstrip("/")
COMPLETION_URL = os.environ.get("ECHO_COMPLETION_URL", INFERENCE_URL.removesuffix("/v1") + "/completion").strip()
INFERENCE_TIMEOUT = float(os.environ.get("ECHO_INFERENCE_TIMEOUT", "120"))


def inference_headers():
    key = os.environ.get("ECHO_INFERENCE_API_KEY", "").strip()
    return {"X-Local-AI-Client": "echo", **({"Authorization": "Bearer " + key} if key else {})}

READER_CONTEXT_DIR = DATA_DIR / "reader-context"
