from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.config import MIN_STARS
from bot.modules.ingestion import RawRepo

MIN_DESCRIPTION_LENGTH = 10
MAX_AGE_DAYS = 14


def _updated_within_days(repo: RawRepo, days: int = MAX_AGE_DAYS) -> bool:
    if not repo.updated_at:
        return False
    try:
        updated = datetime.fromisoformat(repo.updated_at.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return updated >= cutoff
    except (ValueError, TypeError):
        return False


def passes_filter(repo: RawRepo, min_stars: int = MIN_STARS) -> bool:
    return all([
        repo.stars >= min_stars,
        not repo.fork,
        repo.description is not None and len(repo.description.strip()) > MIN_DESCRIPTION_LENGTH,
        _updated_within_days(repo),
        repo.size > 0,
    ])
