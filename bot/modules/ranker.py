from __future__ import annotations

from bot.modules.evaluator import EvalResult

WEIGHT_BUSINESS = 0.50
WEIGHT_NOVELTY = 0.30
WEIGHT_EASE = 0.20


def compute_score(result: EvalResult) -> float:
    return round(
        result.business_potential * WEIGHT_BUSINESS
        + result.novelty * WEIGHT_NOVELTY
        + result.ease_to_mvp * WEIGHT_EASE,
        2,
    )
