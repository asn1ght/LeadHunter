"""Оценка 0–100 «насколько компании нужен новый сайт» (раздел 7.5)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HOT_CODES = frozenset({"site_down", "no_https", "noindex", "mobile_overflow"})


@dataclass
class Weights:
    """Веса оценки. Значения берутся из настроек (SCORE_W_*, SCORE_BONUS_*)."""

    sev3: int = 15
    sev2: int = 8
    sev1: int = 3
    bonus_inn: int = 5
    bonus_segment: int = 5
    bonus_hot: int = 10

    def for_severity(self, sev: int) -> int:
        return {3: self.sev3, 2: self.sev2, 1: self.sev1}.get(sev, 0)


@dataclass
class ScoreResult:
    score: int
    score_tech: int
    score_quality: int | None
    factors: list[dict[str, Any]] = field(default_factory=list)  # [{label, points}]


def compute_score(
    issues: list[dict[str, Any]],
    weights: Weights,
    inn: str | None = None,
    segment: str | None = None,
    priority_segment: str | None = None,
    score_quality: int | None = None,
) -> ScoreResult:
    """Посчитать итоговую оценку и вклад каждого фактора.

    issues: [{code, severity, title}].
    score_tech = сумма весов проблем (макс. 100);
    score = round(0.5*tech + 0.5*quality), без LLM = tech; затем бонусы; итог 0..100.
    """
    factors: list[dict[str, Any]] = []
    tech = 0
    for it in issues:
        pts = weights.for_severity(int(it.get("severity", 0)))
        if pts:
            tech += pts
            factors.append({"label": it.get("title") or it.get("code"), "points": pts})
    tech = min(tech, 100)

    if score_quality is not None:
        base = round(0.5 * tech + 0.5 * score_quality)
        factors.append({"label": f"Качество (ИИ) {score_quality} × 0.5, тех. {tech} × 0.5", "points": 0})
    else:
        base = tech

    score = base
    if inn:
        score += weights.bonus_inn
        factors.append({"label": "Бонус: найден ИНН", "points": weights.bonus_inn})
    if priority_segment and segment and segment == priority_segment:
        score += weights.bonus_segment
        factors.append({"label": f"Бонус: приоритетный сегмент {segment.upper()}", "points": weights.bonus_segment})
    hot = HOT_CODES & {it.get("code") for it in issues}
    if hot:
        score += weights.bonus_hot
        factors.append({"label": "Бонус: горячий лид (" + ", ".join(sorted(hot)) + ")", "points": weights.bonus_hot})

    return ScoreResult(score=max(0, min(score, 100)), score_tech=tech, score_quality=score_quality, factors=factors)
