"""Step 4 -- profile/job match scoring.

Implements exactly the specified formula:

        S_total = alpha * sum_i (w_i * M_i)  +  beta * E  +  gamma * R

    w_i : weight of skill i        (from your weights config)
    M_i : 1 if skill i is in your profile, else 0
    E   : experience alignment score in [0, 1]
    R   : role-type match score in [0, 1]
    alpha, beta, gamma : term weights

Design choices that make the 0.85 threshold meaningful:
  * The skill term is normalized by the total available skill weight for that
    job, so sum_i(w_i*M_i) / sum_i(w_i) is in [0, 1]. Without this, S_total
    would scale with how many skills a posting happens to list.
  * alpha + beta + gamma is expected to equal 1.0 (validated), so S_total is
    in [0, 1] and 0.85 is interpretable as "85% match".

IMPORTANT (deliberate guardrail): jobs with S_total > threshold are written to
a `ready_to_apply` review queue, NOT auto-submitted. A single mis-parse (e.g.
the extractor misreading an experience requirement) should never fire an
application under your name without a human glance. Flip `auto_submit` only if
you fully own that risk.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional


@dataclass
class ScoreWeights:
    alpha: float = 0.6      # weight on the skill term
    beta: float = 0.25      # weight on experience alignment
    gamma: float = 0.15     # weight on role-type match
    # default per-skill weight when a skill isn't in the weights map
    default_skill_weight: float = 1.0
    # importance multipliers applied on top of per-skill weight
    importance_multiplier: Mapping[str, float] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.importance_multiplier is None:
            self.importance_multiplier = {
                "required": 1.0,
                "preferred": 0.6,
                "nice_to_have": 0.3,
            }
        total = self.alpha + self.beta + self.gamma
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"alpha+beta+gamma must equal 1.0 for S_total to stay in [0,1]; "
                f"got {total:.3f}"
            )


@dataclass
class ScoreBreakdown:
    job_key: str
    skill_term: float       # alpha * normalized skill coverage
    exp_term: float         # beta * E
    role_term: float        # gamma * R
    total: float            # S_total

    def as_row(self) -> dict:
        return {
            "job_key": self.job_key,
            "score": round(self.total, 4),
            "skill_term": round(self.skill_term, 4),
            "exp_term": round(self.exp_term, 4),
            "role_term": round(self.role_term, 4),
        }


def experience_alignment(my_years: float, min_years: Optional[int],
                         max_years: Optional[int]) -> float:
    """E in [0,1]. 1.0 when you sit comfortably within the band.

    - No stated requirement -> neutral 0.7 (slight positive; nothing blocks you).
    - At/above the minimum -> 1.0, with a gentle penalty if you're far above the
      max (over-qualification can hurt) but never below 0.5 for that reason.
    - Below the minimum -> linear falloff (you shouldn't usually reach scoring
      since step 3 filters these, but we stay robust).
    """
    if min_years is None and max_years is None:
        return 0.7
    lo = min_years if min_years is not None else 0
    if my_years < lo:
        # 0 at lo-3 years short or worse, up to ~1 as you approach lo.
        gap = lo - my_years
        return max(0.0, 1.0 - gap / 3.0)
    if max_years is not None and my_years > max_years:
        over = my_years - max_years
        return max(0.5, 1.0 - 0.1 * over)
    return 1.0


def role_match(my_role: Optional[str], job_role: Optional[str],
               adjacency: Optional[Mapping[str, set[str]]] = None) -> float:
    """R in [0,1]. Exact role-type match = 1.0; adjacent = 0.6; else 0.2."""
    if not my_role or not job_role:
        return 0.2
    if my_role == job_role:
        return 1.0
    if adjacency and job_role in adjacency.get(my_role, set()):
        return 0.6
    return 0.2


def score_job(
    job_key: str,
    job_skills: Iterable[Mapping],          # [{"skill","importance"}, ...]
    my_skills: set[str],
    *,
    my_years: float,
    min_years: Optional[int],
    max_years: Optional[int],
    my_role: Optional[str],
    job_role: Optional[str],
    weights: ScoreWeights,
    skill_weight_map: Optional[Mapping[str, float]] = None,
    role_adjacency: Optional[Mapping[str, set[str]]] = None,
) -> ScoreBreakdown:
    """Compute S_total for one job."""
    skill_weight_map = skill_weight_map or {}

    # ---- skill term: alpha * ( sum_i w_i*M_i / sum_i w_i ) -----------------
    weighted_present = 0.0
    weight_total = 0.0
    for s in job_skills:
        name = s["skill"]
        imp = s.get("importance", "required")
        w = skill_weight_map.get(name, weights.default_skill_weight)
        w *= weights.importance_multiplier.get(imp, 1.0)
        weight_total += w
        M_i = 1.0 if name in my_skills else 0.0
        weighted_present += w * M_i
    skill_coverage = (weighted_present / weight_total) if weight_total > 0 else 0.0
    skill_term = weights.alpha * skill_coverage

    # ---- experience term: beta * E ----------------------------------------
    E = experience_alignment(my_years, min_years, max_years)
    exp_term = weights.beta * E

    # ---- role term: gamma * R ---------------------------------------------
    R = role_match(my_role, job_role, role_adjacency)
    role_term = weights.gamma * R

    total = skill_term + exp_term + role_term
    return ScoreBreakdown(job_key, skill_term, exp_term, role_term, total)


def partition_by_threshold(
    scores: Iterable[ScoreBreakdown], threshold: float = 0.85
) -> tuple[list[ScoreBreakdown], list[ScoreBreakdown]]:
    """Split into (flagged_for_review, rest). flagged = score > threshold."""
    flagged, rest = [], []
    for sb in scores:
        (flagged if sb.total > threshold else rest).append(sb)
    flagged.sort(key=lambda x: x.total, reverse=True)
    return flagged, rest
