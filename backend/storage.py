# -*- coding: utf-8 -*-
"""Filesystem storage for shared agent avatars.

Avatars are persisted under ``AVATAR_DIR`` as ``<agentId>.<ext>`` so they are
shared across browsers and users (team-wide, "follows the agent"). Images are
returned to the frontend inline as ``data:`` URLs to avoid the ``<img>``
Authorization-header problem.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Dict, Optional

from .constants import (
    AVATAR_DIR,
    EXT_TO_MIME,
    MAX_AVATAR_BYTES,
    MIME_TO_EXT,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$", re.S)


class AvatarError(Exception):
    """Raised for invalid avatar input."""


def is_valid_agent_id(agent_id: str) -> bool:
    """Allow safe identifiers only; block path traversal."""
    if not agent_id or len(agent_id) > 128:
        return False
    if ".." in agent_id or "/" in agent_id or "\\" in agent_id:
        return False
    return bool(_ID_RE.match(agent_id))


def _ensure_dir() -> Path:
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    return AVATAR_DIR


def _find_file(agent_id: str) -> Optional[Path]:
    if not AVATAR_DIR.exists():
        return None
    for ext in MIME_TO_EXT.values():
        candidate = AVATAR_DIR / f"{agent_id}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def _to_data_url(path: Path) -> Optional[str]:
    mime = EXT_TO_MIME.get(path.suffix.lstrip(".").lower())
    if not mime:
        return None
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def list_avatars() -> Dict[str, str]:
    """Return ``{agent_id: data_url}`` for every stored avatar."""
    result: Dict[str, str] = {}
    if not AVATAR_DIR.exists():
        return result
    for path in AVATAR_DIR.iterdir():
        if not path.is_file():
            continue
        ext = path.suffix.lstrip(".").lower()
        if ext not in EXT_TO_MIME:
            continue
        agent_id = path.stem
        if not is_valid_agent_id(agent_id):
            continue
        data_url = _to_data_url(path)
        if data_url:
            result[agent_id] = data_url
    return result


def save_avatar(agent_id: str, data_url: str) -> None:
    """Decode a ``data:`` URL and persist it, replacing any existing avatar."""
    if not is_valid_agent_id(agent_id):
        raise AvatarError("invalid agent id")
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        raise AvatarError("expected a base64 data URL")
    mime = match.group("mime").lower()
    ext = MIME_TO_EXT.get(mime)
    if not ext:
        raise AvatarError(f"unsupported image type: {mime}")
    try:
        raw = base64.b64decode(match.group("data"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise AvatarError("invalid base64 image data") from exc
    if len(raw) > MAX_AVATAR_BYTES:
        raise AvatarError("image too large")

    _ensure_dir()
    # Remove any prior avatar with a different extension to avoid duplicates.
    existing = _find_file(agent_id)
    if existing and existing.suffix.lstrip(".").lower() != ext:
        existing.unlink(missing_ok=True)
    (AVATAR_DIR / f"{agent_id}.{ext}").write_bytes(raw)


def delete_avatar(agent_id: str) -> bool:
    """Remove an agent's avatar. Returns True if a file was deleted."""
    if not is_valid_agent_id(agent_id):
        raise AvatarError("invalid agent id")
    existing = _find_file(agent_id)
    if existing:
        existing.unlink(missing_ok=True)
        return True
    return False
