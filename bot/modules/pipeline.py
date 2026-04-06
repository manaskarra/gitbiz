from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import discord
import httpx
import structlog

from bot.config import MIN_STARS, settings
from bot.modules.dedup import get_top_repos, is_seen, mark_posted, upsert_keep
from bot.modules.discord_poster import build_embed
from bot.modules.evaluator import EvalResult, evaluate_repo
from bot.modules.ingestion import (
    RawRepo,
    _run_search,
    fetch_single_repo,
    search_active_popular,
    search_recent_trending,
)
from bot.modules.prefilter import passes_filter
from bot.modules.ranker import compute_score

logger = structlog.get_logger(__name__)

LLM_DELAY = 1.0


def _github_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _eval_and_post(
    channel: discord.TextChannel | None,
    raw_repo: RawRepo,
    summary: dict,
    stop_on_first: bool = False,
) -> bool:
    if is_seen(raw_repo.url):
        return False

    repo_dict = {
        "name": raw_repo.name,
        "full_name": raw_repo.full_name,
        "url": raw_repo.url,
        "description": raw_repo.description,
        "stars": raw_repo.stars,
        "language": raw_repo.language,
        "last_updated": raw_repo.updated_at,
    }

    summary["evaluated"] += 1
    result = await evaluate_repo(repo_dict)

    if result is None or result.status != "KEEP":
        reason = getattr(result, "reason", "LLM failure") if result else "LLM failure"
        logger.info("rejected", repo=raw_repo.full_name, reason=reason)
        return False

    score = compute_score(result)
    logger.info("keep_found", repo=raw_repo.full_name, score=score)

    if score < settings.min_score_to_post:
        logger.info("keep_below_threshold", repo=raw_repo.full_name, score=score, threshold=settings.min_score_to_post)
        return False

    summary["kept"] += 1
    repo_row = upsert_keep(raw_repo, score, result.to_output_dict())

    if channel is not None and repo_row and summary.get("posted", 0) < settings.max_post_per_run:
        embed_data = {**repo_row, "score": score, "output_json": result.to_output_dict()}
        embed = build_embed(embed_data)
        try:
            await channel.send(embed=embed)
            mark_posted(repo_row["id"])
            summary["posted"] = summary.get("posted", 0) + 1
            logger.info("discord_posted", repo=raw_repo.full_name, score=score)
        except discord.HTTPException as exc:
            logger.error("discord_post_error", repo=raw_repo.full_name, error=str(exc))

    return stop_on_first


async def run(channel: discord.TextChannel | None = None, keywords: list[str] | None = None) -> dict:
    summary = {"evaluated": 0, "kept": 0, "posted": 0, "keywords_used": []}

    if keywords:
        sampled = keywords
    else:
        now = datetime.now(timezone.utc)
        all_kws = settings.search_keywords[:]
        week_number = now.isocalendar()[1]
        random.seed(week_number)
        random.shuffle(all_kws)
        random.seed()
        day_of_week = now.weekday()
        start = (day_of_week * 10) % len(all_kws)
        sampled = [all_kws[i % len(all_kws)] for i in range(start, start + 10)]

    summary["keywords_used"] = sampled
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")

    async with httpx.AsyncClient(headers=_github_headers(), timeout=30.0) as client:
        for kw in sampled:
            if summary["posted"] >= settings.max_post_per_run:
                break
            query = f"{kw} stars:>={MIN_STARS} created:>{cutoff} fork:false"
            candidates = await _run_search(client, query, sort="stars", label=f"kw:{kw}")
            eligible = [r for r in candidates if passes_filter(r, MIN_STARS)][: settings.repos_per_keyword]
            for raw_repo in eligible:
                await _eval_and_post(channel, raw_repo, summary)

    logger.info("pipeline_complete", **{k: v for k, v in summary.items() if k != "keywords_used"})
    return summary


async def run_quick(
    channel: discord.TextChannel,
    keyword: str | None = None,
    mode: str = "keyword",
    post_limit: int = 1,
) -> dict:
    summary = {"evaluated": 0, "kept": 0, "posted": 0}

    if mode == "trending":
        candidates = await search_recent_trending()
    elif mode == "popular":
        candidates = await search_active_popular()
    else:
        if keyword:
            sampled_keywords = [keyword.strip()]
        else:
            sampled_keywords = random.sample(settings.search_keywords, min(3, len(settings.search_keywords)))

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        candidates = []
        async with httpx.AsyncClient(headers=_github_headers(), timeout=30.0) as client:
            for kw in sampled_keywords:
                query = f"{kw} stars:>={MIN_STARS} created:>{cutoff} fork:false"
                candidates.extend(await _run_search(client, query, sort="stars", label=f"quick:{kw}"))

    for raw_repo in candidates:
        if summary["posted"] >= post_limit:
            break
        if summary["evaluated"] >= settings.quick_max_eval:
            logger.info("quick_eval_cap_reached", cap=settings.quick_max_eval)
            break
        if not passes_filter(raw_repo, MIN_STARS):
            continue
        stop = post_limit == 1
        found = await _eval_and_post(channel, raw_repo, summary, stop_on_first=stop)
        if found:
            break

    logger.info("quick_scan_complete", **summary)
    return summary


async def evaluate_single(repo_url: str) -> tuple[dict, EvalResult | None, float | None]:
    parts = repo_url.rstrip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {repo_url}")
    owner_repo = f"{parts[-2]}/{parts[-1]}"

    raw = await fetch_single_repo(owner_repo)
    if raw is None:
        raise ValueError(f"Could not fetch repo: {owner_repo}")

    repo_dict = {
        "name": raw.name,
        "full_name": raw.full_name,
        "url": raw.url,
        "description": raw.description,
        "stars": raw.stars,
        "language": raw.language,
        "last_updated": raw.updated_at,
    }

    result = await evaluate_repo(repo_dict)
    score = compute_score(result) if result and result.status == "KEEP" else None

    if result and result.status == "KEEP" and score is not None:
        upsert_keep(raw, score, result.to_output_dict())

    return repo_dict, result, score
