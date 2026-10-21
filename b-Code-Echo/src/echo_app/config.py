"""Echo 的固定配置：数据目录、私有引擎端口、默认模型。"""

import os
from pathlib import Path

# 引擎和模型都放在 Echo 自己的数据目录，不碰本机已装的 Ollama。
# 不放 %LOCALAPPDATA%：从打包应用（如 Claude 桌面版）里启动的进程写 AppData 会被重定向到
# 应用私有目录，快捷方式启动的 Echo 就看不到。和 Ollama 的 ~/.ollama 一样放用户目录。
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

# 私有实例的端口，和本机 Ollama 默认的 11434 错开。
ENGINE_HOST = "127.0.0.1"
ENGINE_PORT = int(os.environ.get("ECHO_ENGINE_PORT", "11540"))
ENGINE_URL = f"http://{ENGINE_HOST}:{ENGINE_PORT}"

WEB_PORT = int(os.environ.get("ECHO_WEB_PORT", "8040"))

MODEL = os.environ.get("ECHO_MODEL", "qwen3:8b")
KEEP_ALIVE = "30m"
# 引擎并行槽数：输入预测的 5 个候选同时补全。每个槽各占一份 KV 缓存（4096 上下文约 0.6GB）。
ENGINE_PARALLEL = 5

SYSTEM_PROMPT = (
    "You are Echo, a patient English conversation partner for a Chinese learner "
    "whose English is not yet fluent. Always reply in simple, natural English, "
    "in two or three short sentences, and keep the conversation going with a question."
)
