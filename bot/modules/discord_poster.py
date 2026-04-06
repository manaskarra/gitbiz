from __future__ import annotations

from datetime import datetime, timezone

import discord
import structlog

logger = structlog.get_logger(__name__)

FOOTER_TEXT = "GitBiz • github.com/manaskarra"

SCORE_COLORS = {
    8: discord.Colour.green(),
    6: discord.Colour.gold(),
    0: discord.Colour.orange(),
}


def _trunc(text: str, limit: int = 900) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _score_block(output: dict) -> tuple[int, int, int]:
    scores = output.get("scores") or {}
    return (
        int(scores.get("business_potential", output.get("business_potential", 0) or 0)),
        int(scores.get("novelty", output.get("novelty", 0) or 0)),
        int(scores.get("ease_of_mvp", output.get("ease_to_mvp", output.get("ease_of_mvp", 0)) or 0)),
    )


def _color_for_score(score: float) -> discord.Colour:
    for threshold, color in SCORE_COLORS.items():
        if score >= threshold:
            return color
    return discord.Colour.light_grey()


def build_embed(repo: dict) -> discord.Embed:
    output = repo.get("output_json") or {}
    score = float(repo.get("score", 0) or 0)

    title = repo.get("full_name", repo.get("name", "Unknown"))
    url = repo.get("url", "")

    now = datetime.now(timezone.utc)

    if output.get("status") == "REJECT":
        rej_kw: dict = {
            "title": title,
            "description": _trunc(output.get("reason", "Rejected"), 2000),
            "colour": discord.Colour.dark_grey(),
        }
        if url:
            rej_kw["url"] = url
        embed = discord.Embed(**rej_kw)
        embed.add_field(name="Verdict", value="**REJECT**", inline=False)
        embed.set_footer(text=FOOTER_TEXT)
        embed.timestamp = now
        return embed

    bp, nov, ease = _score_block(output)
    idea = output.get("product_idea") or output.get("opportunity") or ""

    keep_kw: dict = {
        "title": title,
        "description": _trunc(output.get("summary", repo.get("description", "")), 2000),
        "colour": _color_for_score(score),
    }
    if url:
        keep_kw["url"] = url
    embed = discord.Embed(**keep_kw)
    embed.add_field(name="Score", value=f"**{score:.1f}** / 10", inline=True)
    embed.add_field(name="Stars", value=f"{repo.get('stars', 0):,}", inline=True)
    embed.add_field(name="Language", value=repo.get("language") or "N/A", inline=True)

    if idea:
        embed.add_field(name="Product idea", value=_trunc(idea), inline=False)

    target = output.get("target_user") or output.get("target_customer") or ""
    if target:
        embed.add_field(name="Target", value=_trunc(target), inline=False)

    if output.get("monetization"):
        embed.add_field(name="Monetization", value=_trunc(output["monetization"]), inline=False)

    feats = output.get("features") or []
    if feats:
        lines = "\n".join(f"• {_trunc(str(f), 200)}" for f in feats[:5])
        embed.add_field(name="Features", value=_trunc(lines, 1000), inline=False)

    scores = output.get("scores") or {}
    confidence = scores.get("confidence")
    confidence_str = f" | Confidence: {confidence}/10" if confidence else ""
    embed.add_field(
        name="Scores",
        value=f"Business: {bp}/10 | Novelty: {nov}/10 | Ease MVP: {ease}/10{confidence_str}",
        inline=False,
    )

    if output.get("hidden_capability"):
        embed.add_field(name="Hidden capability", value=_trunc(output["hidden_capability"]), inline=False)

    embed.set_footer(text=FOOTER_TEXT)
    embed.timestamp = now
    return embed
