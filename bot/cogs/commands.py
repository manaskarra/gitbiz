from __future__ import annotations

import discord
from discord import option
from discord.ext import commands

import structlog

from bot.config import settings
from bot.modules import pipeline
from bot.modules.dedup import get_top_repos
from bot.modules.discord_poster import build_embed

logger = structlog.get_logger(__name__)

SCAN_COOLDOWN = 30
CRONTEST_COOLDOWN = 600
REPO_COOLDOWN = 30
TRENDING_COOLDOWN = 60
POPULAR_COOLDOWN = 60


class OpportunityCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @discord.slash_command(name="scan", description="Quick scan: evaluate repos one by one until a KEEP is found")
    @option("keyword", description="Topic to scan for (optional — random if omitted)", type=str, required=False, default=None)
    @commands.cooldown(1, SCAN_COOLDOWN, commands.BucketType.guild)
    async def scan(self, ctx: discord.ApplicationContext, keyword: str | None = None) -> None:
        await ctx.defer()
        channel = self.bot.get_channel(settings.discord_channel_id)
        if channel is None:
            await ctx.followup.send("Configured channel not found. Check DISCORD_CHANNEL_ID.")
            return

        topic = f"`{keyword}`" if keyword else "random topics"
        await ctx.followup.send(f"Quick scan running — searching {topic}...")
        try:
            summary = await pipeline.run_quick(channel, keyword=keyword)
            if not summary["kept"]:
                await ctx.followup.send(
                    f"No new opportunities this scan ({summary['evaluated']} evaluated). Try again later."
                )
        except Exception as exc:
            logger.error("scan_command_error", error=str(exc))
            await ctx.followup.send(f"Scan failed: {type(exc).__name__}")

    @discord.slash_command(name="trending", description="Scan recently created repos with fast star growth (last 7 days)")
    @commands.cooldown(1, TRENDING_COOLDOWN, commands.BucketType.guild)
    async def trending(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        channel = self.bot.get_channel(settings.discord_channel_id)
        if channel is None:
            await ctx.followup.send("Configured channel not found. Check DISCORD_CHANNEL_ID.")
            return

        await ctx.followup.send("Trending scan running — searching recently created repos with fast star growth...")
        try:
            summary = await pipeline.run_quick(channel, mode="trending", post_limit=3)
            if not summary["kept"]:
                await ctx.followup.send(
                    f"No new opportunities found ({summary['evaluated']} evaluated). Try again later."
                )
        except Exception as exc:
            logger.error("trending_command_error", error=str(exc))
            await ctx.followup.send(f"Trending scan failed: {type(exc).__name__}")

    @discord.slash_command(name="popular", description="Scan established repos (≥100 stars) with active recent pushes")
    @commands.cooldown(1, POPULAR_COOLDOWN, commands.BucketType.guild)
    async def popular(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        channel = self.bot.get_channel(settings.discord_channel_id)
        if channel is None:
            await ctx.followup.send("Configured channel not found. Check DISCORD_CHANNEL_ID.")
            return

        await ctx.followup.send("Popular scan running — searching established repos with active recent pushes...")
        try:
            summary = await pipeline.run_quick(channel, mode="popular", post_limit=3)
            if not summary["kept"]:
                await ctx.followup.send(
                    f"No new opportunities found ({summary['evaluated']} evaluated). Try again later."
                )
        except Exception as exc:
            logger.error("popular_command_error", error=str(exc))
            await ctx.followup.send(f"Popular scan failed: {type(exc).__name__}")

    @discord.slash_command(name="crontest", description="Manually trigger today's cron — same 10-keyword daily cycle as the scheduled job")
    @commands.cooldown(1, CRONTEST_COOLDOWN, commands.BucketType.guild)
    async def crontest(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        channel = self.bot.get_channel(settings.discord_channel_id)
        if channel is None:
            await ctx.followup.send("Configured channel not found. Check DISCORD_CHANNEL_ID.")
            return

        await ctx.followup.send("Cron test started — running today's 10-keyword cycle. KEEPs will appear as they're found...")
        try:
            summary = await pipeline.run(channel)
            kws = ", ".join(summary.get("keywords_used", []))
            await ctx.followup.send(
                f"Cron test complete — keywords: `{kws}`\n"
                f"Evaluated: {summary['evaluated']} | "
                f"Kept: {summary['kept']} | "
                f"Posted: {summary['posted']}"
            )
        except Exception as exc:
            logger.error("crontest_command_error", error=str(exc))
            await ctx.followup.send(f"Cron test failed: {type(exc).__name__}")

    @discord.slash_command(name="keywords", description="Show all configured discovery keywords")
    async def keywords(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        kws = settings.search_keywords
        formatted = ", ".join(f"`{k}`" for k in kws)
        await ctx.followup.send(
            f"**{len(kws)} configured keywords:**\n{formatted}\n\n"
            f"Daily cron cycles through 10 per day (full coverage every 7 days). "
            f"`/crontest` triggers today's cycle manually. "
            f"`/scan <keyword>` accepts any custom keyword."
        )

    @discord.slash_command(name="top", description="Show highest-scoring opportunity repos")
    @option("count", description="Number of repos to show", type=int, default=5, min_value=1, max_value=20)
    async def top(self, ctx: discord.ApplicationContext, count: int = 5) -> None:
        await ctx.defer()
        repos = get_top_repos(count)
        if not repos:
            await ctx.followup.send("No evaluated repos yet. Run /scan first.")
            return

        await ctx.followup.send(f"Top {len(repos)} opportunities:")
        for repo in repos:
            embed = build_embed(repo)
            await ctx.followup.send(embed=embed)

    @discord.slash_command(name="repo", description="Analyze a specific GitHub repo for opportunities")
    @option("url", description="GitHub repository URL", type=str, required=True)
    @commands.cooldown(1, REPO_COOLDOWN, commands.BucketType.user)
    async def repo(self, ctx: discord.ApplicationContext, url: str) -> None:
        await ctx.defer()

        if "github.com" not in url:
            await ctx.followup.send("Please provide a valid GitHub URL.")
            return

        try:
            repo_row, result, score = await pipeline.evaluate_single(url)
        except ValueError as exc:
            await ctx.followup.send(str(exc))
            return
        except Exception as exc:
            logger.error("repo_command_error", url=url, error=str(exc))
            await ctx.followup.send(f"Analysis failed: {type(exc).__name__}")
            return

        if result is None:
            await ctx.followup.send("LLM evaluation failed for this repo. Try again later.")
            return

        embed_data = {**repo_row, "score": score, "output_json": result.to_output_dict()}
        embed = build_embed(embed_data)
        status_label = "KEEP" if result.status == "KEEP" else "REJECT"
        await ctx.followup.send(f"Verdict: **{status_label}**", embed=embed)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(OpportunityCog(bot))
