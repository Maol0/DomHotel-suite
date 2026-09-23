# -*- coding: utf-8 -*-
"""HTTP routes for shared agent avatars."""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import storage
from .storage import AvatarError

logger = logging.getLogger("qwenpaw.agent_office")


class AvatarMap(BaseModel):
    """Mapping of agent id -> avatar data URL."""

    avatars: Dict[str, str]


class AvatarUpload(BaseModel):
    """Avatar upload payload (a base64 ``data:`` URL)."""

    data_url: str


def create_router() -> APIRouter:
    """Build the Agent Office API router."""
    router = APIRouter()

    @router.get("/avatars", response_model=AvatarMap)
    def get_avatars() -> AvatarMap:
        """Return every stored avatar inline as a data URL."""
        return AvatarMap(avatars=storage.list_avatars())

    @router.put("/avatars/{agent_id}", response_model=AvatarMap)
    def put_avatar(agent_id: str, payload: AvatarUpload) -> AvatarMap:
        """Store (or replace) an agent's avatar."""
        try:
            storage.save_avatar(agent_id, payload.data_url)
        except AvatarError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save avatar: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="save failed") from exc
        return AvatarMap(avatars=storage.list_avatars())

    @router.delete("/avatars/{agent_id}", response_model=AvatarMap)
    def remove_avatar(agent_id: str) -> AvatarMap:
        """Delete an agent's avatar (no error if it does not exist)."""
        try:
            storage.delete_avatar(agent_id)
        except AvatarError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return AvatarMap(avatars=storage.list_avatars())

    return router
