from __future__ import annotations

import structlog
from supabase import Client, create_client

from bot.config import settings

logger = structlog.get_logger(__name__)

_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_key)
        logger.info("supabase_client_initialized")
    return _client
