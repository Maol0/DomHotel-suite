# -*- coding: utf-8 -*-
"""Constants for the Agent Office backend."""

from __future__ import annotations

from pathlib import Path

# Mounted under /api + API_PREFIX (e.g. /api/agent-office/avatars).
API_PREFIX = "/agent-office"

# Where shared (team-wide) agent avatars are stored on disk.
try:
    from qwenpaw.constant import WORKING_DIR

    AVATAR_DIR: Path = Path(WORKING_DIR) / "agent_office" / "avatars"
except Exception:  # pragma: no cover - fallback when constant unavailable
    AVATAR_DIR = Path.home() / ".qwenpaw" / "agent_office" / "avatars"

# Accepted image types and the extension used to store each.
MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
EXT_TO_MIME = {ext: mime for mime, ext in MIME_TO_EXT.items()}

# Reject avatars larger than this (bytes) after base64 decoding.
MAX_AVATAR_BYTES = 3 * 1024 * 1024
