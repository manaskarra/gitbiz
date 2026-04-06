from __future__ import annotations

import logging
import os
import ssl
from typing import Any

import certifi


def _use_certifi_for_ssl() -> None:
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    _orig = ssl.create_default_context

    def _default_with_certifi(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        if args:
            return _orig(*args, **kwargs)
        kwargs = dict(kwargs)
        kwargs.setdefault("cafile", certifi.where())
        return _orig(**kwargs)

    ssl.create_default_context = _default_with_certifi  # type: ignore[method-assign]


_use_certifi_for_ssl()

import discord
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import settings
from bot.modules import pipeline

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(min_level=logging.INFO),
)

logger = structlog.get_logger(__name__)

intents = discord.Intents.default()
bot = discord.Bot(intents=intents)
scheduler = AsyncIOScheduler()


async def _scheduled_full_pipeline() -> None:
    channel = bot.get_channel(settings.discord_channel_id)
    if channel is None:
        logger.error("scheduled_pipeline_no_channel", channel_id=settings.discord_channel_id)
        return
    logger.info("scheduled_pipeline_start")
    try:
        summary = await pipeline.run(channel)
        kws = ", ".join(summary.get("keywords_used", []))
        await channel.send(
            f"Daily keyword scan complete — keywords today: `{kws}`\n"
            f"Evaluated: {summary['evaluated']} | Kept: {summary['kept']} | Posted: {summary['posted']}"
        )
        logger.info("scheduled_pipeline_done", **{k: v for k, v in summary.items() if k != "keywords_used"})
    except Exception as exc:
        logger.error("scheduled_pipeline_error", error=str(exc))


async def _scheduled_trending() -> None:
    channel = bot.get_channel(settings.discord_channel_id)
    if channel is None:
        logger.error("scheduled_trending_no_channel", channel_id=settings.discord_channel_id)
        return
    logger.info("scheduled_trending_start")
    try:
        summary = await pipeline.run_quick(channel, mode="trending", post_limit=2)
        logger.info("scheduled_trending_done", **summary)
    except Exception as exc:
        logger.error("scheduled_trending_error", error=str(exc))


async def _scheduled_popular() -> None:
    channel = bot.get_channel(settings.discord_channel_id)
    if channel is None:
        logger.error("scheduled_popular_no_channel", channel_id=settings.discord_channel_id)
        return
    logger.info("scheduled_popular_start")
    try:
        summary = await pipeline.run_quick(channel, mode="popular", post_limit=2)
        logger.info("scheduled_popular_done", **summary)
    except Exception as exc:
        logger.error("scheduled_popular_error", error=str(exc))


@bot.event
async def on_ready() -> None:
    logger.info("bot_ready", user=str(bot.user), guilds=len(bot.guilds))
    await bot.sync_commands()
    logger.info("commands_synced")
    scheduler.add_job(
        _scheduled_full_pipeline,
        CronTrigger(hour=6, minute=0, timezone="UTC"),
        id="daily_pipeline",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_trending,
        CronTrigger(hour=10, minute=0, timezone="UTC"),
        id="trending_pipeline",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_popular,
        CronTrigger(hour=14, minute=0, timezone="UTC"),
        id="popular_pipeline",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler_started", jobs=["06:00 UTC keyword scan", "10:00 UTC trending", "14:00 UTC popular"])


def main() -> None:
    bot.load_extension("bot.cogs.commands")
    logger.info("starting_bot")
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
