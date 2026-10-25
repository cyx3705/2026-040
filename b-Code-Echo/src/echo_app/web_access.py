"""NET-001: opt-in extension access; the desktop UI stays local."""

import ipaddress
import os
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config

EXTENSION_ROUTES = {
    ("GET", "/api/status"),
    ("GET", "/api/reader/learned"),
    ("POST", "/api/reader/gloss"),
    ("POST", "/api/learned"),
}


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        return (getattr(address, "ipv4_mapped", None) or address).is_loopback
    except ValueError:
        return False


def validate_key(key: str) -> str:
    if len(key) < 32 or len(key) > 256 or not key.isascii() or not all(33 <= ord(c) <= 126 for c in key):
        raise ValueError("共享密钥需要 32～256 个 ASCII 非空白字符")
    return key


def load_key(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return validate_key(path.read_text(encoding="utf-8").strip())
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        key = secrets.token_urlsafe(32)
        target.write(key + "\n")
    return key


def configure(app) -> None:
    app.state.share_key = None if loopback(config.WEB_HOST) else (
        validate_key(config.SHARE_KEY) if config.SHARE_KEY else load_key(config.SHARE_KEY_FILE))


class ExtensionAccessMiddleware:
    """Pure ASGI: preserve streamed SSE and disconnect/cancellation semantics."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        origin = request.headers.get("origin")
        local = (request.client is not None and loopback(request.client.host)
                 and loopback(request.url.hostname or "")
                 and (not origin or origin == f"{request.url.scheme}://{request.url.netloc}"
                      or origin.startswith("chrome-extension://")))
        if not local:
            key = getattr(scope["app"].state, "share_key", None)
            authorization = request.headers.get("authorization", "")
            if not key or not secrets.compare_digest(authorization.encode(), f"Bearer {key}".encode()):
                return await JSONResponse({"detail": "extension_auth_required"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})(scope, receive, send)
            if (request.method, request.url.path) not in EXTENSION_ROUTES:
                return await JSONResponse({"detail": "extension_route_only"}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)
