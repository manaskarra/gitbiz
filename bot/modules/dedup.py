from __future__ import annotations

import structlog

from bot.db.client import get_supabase
from bot.modules.ingestion import RawRepo

logger = structlog.get_logger(__name__)


def is_seen(url: str) -> bool:
    client = get_supabase()
    result = client.table("repos").select("id").eq("url", url).limit(1).execute()
    return bool(result.data)


def upsert_keep(raw: RawRepo, score: float, output_json: dict) -> dict | None:
    client = get_supabase()
    row = {
        "name": raw.name,
        "full_name": raw.full_name,
        "url": raw.url,
        "description": raw.description,
        "stars": raw.stars,
        "language": raw.language,
        "last_updated": raw.updated_at if raw.updated_at else None,
        "processed": True,
        "llm_status": "KEEP",
        "score": score,
        "output_json": output_json,
    }
    result = (
        client.table("repos")
        .upsert(row, on_conflict="url", ignore_duplicates=True)
        .execute()
    )
    inserted = result.data[0] if result.data else None
    if inserted:
        logger.info("keep_stored", repo=raw.full_name, score=score)
    return inserted


def mark_posted(repo_id: int) -> None:
    client = get_supabase()
    client.table("repos").update({"discord_posted": True}).eq("id", repo_id).execute()


def get_top_repos(limit: int = 5) -> list[dict]:
    client = get_supabase()
    result = (
        client.table("repos")
        .select("*")
        .eq("llm_status", "KEEP")
        .order("score", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []
