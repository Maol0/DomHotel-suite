# -*- coding: utf-8 -*-
"""QwenPaw Agent Office plugin entry point.

Registers a small HTTP API for team-shared agent avatars so they are stored
server-side (persist across browsers, "follow the agent") instead of only in
each browser's localStorage.
"""

from __future__ import annotations

import logging

from qwenpaw.plugins.api import PluginApi

from .backend.constants import API_PREFIX
from .backend.router import create_router

logger = logging.getLogger("qwenpaw.agent_office")


class AgentOfficePlugin:
    """Registers the Agent Office backend API."""

    def register(self, api: PluginApi) -> None:
        api.register_http_router(
            create_router(),
            prefix=API_PREFIX,
            tags=["agent-office"],
        )
        logger.info("Agent Office plugin registered (prefix=%s)", API_PREFIX)


plugin = AgentOfficePlugin()
