"""echo-setup: install the offline dictionary only (EXT-001)."""
from pathlib import Path
import httpx
from . import config
from .dictionary import build_db


def download(url: str, dst: Path, chunk: int = 8 << 20, retries: int = 20) -> None:
    """分段续传下载：本机出网大传输会断，靠 Range 分段加重试，而不是加超时。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    # identity：不让服务器压缩，Range 的字节偏移才对得上文件本身
    with httpx.Client(
        follow_redirects=True, timeout=30, headers={"Accept-Encoding": "identity"}
    ) as client:
        total = int(client.head(url).headers["content-length"])
        failures = 0
        while (done := part.stat().st_size if part.exists() else 0) < total:
            end = min(done + chunk, total) - 1
            try:
                resp = client.get(url, headers={"Range": f"bytes={done}-{end}"})
                resp.raise_for_status()
                with open(part, "ab") as f:
                    f.write(resp.content)
                failures = 0
                print(f"\r下载 {done * 100 // total}%", end="", flush=True)
            except httpx.HTTPError as exc:
                failures += 1
                if failures > retries:
                    raise RuntimeError(f"下载反复失败：{exc}") from exc
    print()
    part.replace(dst)


def install_dictionary() -> None:
    if config.DICT_DB.is_file():
        print(f"词典已在 {config.DICT_DB}")
        return
    csv_path = config.DICT_DB.with_name("ecdict.csv")
    if not csv_path.is_file():
        print("下载 ECDICT 词典（约 66MB）")
        download(config.DICT_CSV_URL, csv_path, chunk=4 << 20)
    print("建立词典索引…")
    rows = build_db(csv_path, config.DICT_DB)
    csv_path.unlink()
    print(f"词典就绪：{rows} 条")


def main():
    install_dictionary()
    print("Echo 词典已就绪。请在外部服务管理模型，配置 ECHO_INFERENCE_URL 后运行 echo-serve。")
