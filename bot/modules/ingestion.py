from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from bot.config import settings

logger = structlog.get_logger(__name__)

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_REPO_URL = "https://api.github.com/repos"
MAX_PER_PAGE = 100
RATE_LIMIT_PAUSE = 2.5
README_EXCERPT_MAX_CHARS = 6000


def _github_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def fetch_readme_excerpt(full_name: str, max_chars: int = README_EXCERPT_MAX_CHARS) -> str:
    if not full_name or "/" not in full_name:
        return "(Invalid repo name.)"

    async with httpx.AsyncClient(headers=_github_headers(), timeout=25.0) as client:
        try:
            resp = await client.get(f"{GITHUB_REPO_URL}/{full_name}/readme")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return "(No README found.)"
            logger.warning("github_readme_error", repo=full_name, status=exc.response.status_code)
            return "(README unavailable.)"
        except httpx.RequestError as exc:
            logger.warning("github_readme_request_error", repo=full_name, error=str(exc))
            return "(README unavailable.)"

    b64 = data.get("content") or ""
    try:
        raw = base64.b64decode(b64).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return "(README decode failed.)"

    raw = raw.strip()
    if not raw:
        return "(Empty README.)"
    if len(raw) > max_chars:
        return raw[:max_chars] + "\n\n[...truncated...]"
    return raw


@dataclass
class RawRepo:
    name: str
    full_name: str
    url: str
    description: str | None
    stars: int
    language: str | None
    updated_at: str
    fork: bool
    size: int


def _parse_repo_item(item: dict) -> RawRepo:
    return RawRepo(
        name=item.get("name", ""),
        full_name=item.get("full_name", ""),
        url=item.get("html_url", ""),
        description=item.get("description"),
        stars=item.get("stargazers_count", 0),
        language=item.get("language"),
        updated_at=item.get("updated_at", ""),
        fork=item.get("fork", False),
        size=item.get("size", 0),
    )


async def _run_search(
    client: httpx.AsyncClient,
    query: str,
    sort: str = "stars",
    order: str = "desc",
    seen_urls: set[str] | None = None,
    label: str = "",
) -> list[RawRepo]:
    if seen_urls is None:
        seen_urls = set()

    params = {"q": query, "sort": sort, "order": order, "per_page": MAX_PER_PAGE}
    try:
        resp = await client.get(GITHUB_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("github_search_error", label=label, status=exc.response.status_code)
        await asyncio.sleep(RATE_LIMIT_PAUSE)
        return []
    except httpx.RequestError as exc:
        logger.warning("github_request_error", label=label, error=str(exc))
        await asyncio.sleep(RATE_LIMIT_PAUSE)
        return []

    items = data.get("items", [])
    results: list[RawRepo] = []
    for item in items:
        url = item.get("html_url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append(_parse_repo_item(item))

    logger.info("github_search_results", label=label, count=len(items), new=len(results))
    await asyncio.sleep(RATE_LIMIT_PAUSE)
    return results


async def search_recent_trending(min_stars: int = 10) -> list[RawRepo]:
    cutoff_7 = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    seen_urls: set[str] = set()
    results: list[RawRepo] = []
    async with httpx.AsyncClient(headers=_github_headers(), timeout=30.0) as client:
        for threshold in (min_stars, 50):
            query = f"stars:>={threshold} created:>{cutoff_7} fork:false"
            new = await _run_search(client, query, sort="stars", seen_urls=seen_urls, label=f"trending:stars>={threshold}")
            results.extend(new)
    return results


async def search_active_popular() -> list[RawRepo]:
    cutoff_3 = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    async with httpx.AsyncClient(headers=_github_headers(), timeout=30.0) as client:
        return await _run_search(
            client,
            f"stars:>=100 pushed:>{cutoff_3} fork:false",
            sort="updated",
            label="active:popular",
        )


async def fetch_single_repo(owner_repo: str) -> RawRepo | None:
    headers = _github_headers()

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        try:
            resp = await client.get(f"{GITHUB_REPO_URL}/{owner_repo}")
            resp.raise_for_status()
            item = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning("github_fetch_error", repo=owner_repo, error=str(exc))
            return None

    return RawRepo(
        name=item.get("name", ""),
        full_name=item.get("full_name", ""),
        url=item.get("html_url", ""),
        description=item.get("description"),
        stars=item.get("stargazers_count", 0),
        language=item.get("language"),
        updated_at=item.get("updated_at", ""),
        fork=item.get("fork", False),
        size=item.get("size", 0),
    )
