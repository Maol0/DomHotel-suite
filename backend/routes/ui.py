# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — UI 静态资源 serve

为什么需要这个:
  plugin-entry.js 在 console 里渲染 iframe,
  iframe src 必须是 /api/hotel-frontdesk-pawapp/ui/ 这种同源 URL
  (用 window.QwenPaw.host.getApiUrl() 拼出来),
  不能是 /pawapps/{id}/static/ui/ 这种路径 (QwenPaw 内部静态服务
  不一定在每个版本都启用/同路径)。

  这个 module 用 FastAPI router 直接 serve ../ui/ 目录里的文件,
  保证任何 QwenPaw 部署都能拿到 SPA 入口 HTML 和静态资源。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# UI_DIR = <plugin>/ui/  (与本文件同级的 ../ui/)
_THIS_DIR = Path(__file__).resolve().parent
UI_DIR = (_THIS_DIR.parent.parent / "ui").resolve()

_MIME = {
    ".html": "text/html",
    ".htm": "text/html",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".ico": "image/x-icon",
}


def _iter_file(path: Path, chunk: int = 64 * 1024):
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            yield data


@router.get("/ui", include_in_schema=False)
@router.get("/ui/", include_in_schema=False)
async def ui_index():
    """SPA 入口 HTML"""
    html_path = UI_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Hotel Frontdesk</title>"
            "<body style='font-family:sans-serif;padding:20px'>"
            "<h1>Hotel Frontdesk UI 缺失</h1>"
            "<p>请确认 plugin/ui/index.html 存在。</p></body>",
            status_code=200,
        )
    return StreamingResponse(
        _iter_file(html_path),
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/ui/{file_path:path}", include_in_schema=False)
async def ui_static(file_path: str):
    """Serve UI 静态资源 (CSS / JS / 图片等)"""
    safe = (UI_DIR / file_path).resolve()
    # 防路径穿越
    try:
        safe.relative_to(UI_DIR)
    except ValueError:
        raise HTTPException(status_code=403, detail="forbidden")
    if not safe.exists() or not safe.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {file_path}")
    suffix = safe.suffix.lower()
    media = _MIME.get(suffix, "application/octet-stream")
    return StreamingResponse(
        _iter_file(safe),
        media_type=media,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
