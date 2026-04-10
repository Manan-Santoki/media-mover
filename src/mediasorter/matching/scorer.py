"""Confidence scoring for TMDB matches.

Scores how well a TMDB result matches a parsed filename using:
- Title similarity (rapidfuzz): 50% weight
- Year match: 25% weight
- Type agreement: 15% weight
- Popularity (log-scaled): 10% weight
"""

from __future__ import annotations

import math

from rapidfuzz import fuzz

from mediasorter.matching.tmdb_client import TMDBResult
from mediasorter.parsing.guessit_wrapper import ParsedMedia
from mediasorter.parsing.normalize import normalize_for_comparison


def score_match(parsed: ParsedMedia, result: TMDBResult) -> float:
    """Return confidence score 0.0-1.0 for how well a TMDB result matches."""
    title_score = _title_similarity(parsed.title, result.title, result.original_title)
    year_score = _year_score(parsed.year, result.year)
    type_score = _type_agreement(parsed.media_type, result.media_type)
    popularity_score = _popularity_score(result.popularity)

    # Weighted combination
    return (
        0.50 * title_score
        + 0.25 * year_score
        + 0.15 * type_score
        + 0.10 * popularity_score
    )


def best_match(
    parsed: ParsedMedia,
    results: list[TMDBResult],
    threshold: float = 0.85,
) -> tuple[TMDBResult | None, float]:
    """Return the highest-scoring match above threshold.

    Returns (match, score) or (None, 0.0) if no match meets the threshold.
    """
    if not results:
        return None, 0.0

    scored = [(r, score_match(parsed, r)) for r in results]
    scored.sort(key=lambda x: x[1], reverse=True)

    best_result, best_score = scored[0]
    if best_score >= threshold:
        return best_result, best_score

    return None, best_score


def _title_similarity(parsed_title: str, tmdb_title: str, tmdb_original: str) -> float:
    """Compute normalized title similarity using rapidfuzz.

    Compares against both the localized and original title, taking the best.
    Uses token_sort_ratio to handle word reordering.
    """
    parsed_norm = normalize_for_comparison(parsed_title)
    title_norm = normalize_for_comparison(tmdb_title)
    original_norm = normalize_for_comparison(tmdb_original)

    score_title = fuzz.token_sort_ratio(parsed_norm, title_norm) / 100.0
    score_original = fuzz.token_sort_ratio(parsed_norm, original_norm) / 100.0

    return max(score_title, score_original)


def _year_score(parsed_year: int | None, tmdb_year: int | None) -> float:
    """Score year match.

    Exact match: 1.0
    Off by 1: 0.6  (handles Dec release vs Jan air date)
    No year in filename: 0.4  (partial credit, no penalty)
    Off by 2+: 0.0
    """
    if parsed_year is None:
        return 0.4

    if tmdb_year is None:
        return 0.2

    diff = abs(parsed_year - tmdb_year)
    if diff == 0:
        return 1.0
    elif diff == 1:
        return 0.6
    else:
        return 0.0


def _type_agreement(parsed_type: str, tmdb_type: str) -> float:
    """Score type agreement (movie vs tv/episode).

    Matching types: 1.0
    Mismatched: 0.0
    """
    # Normalize: our parsed type is "movie"/"episode", TMDB uses "movie"/"tv"
    parsed_norm = "tv" if parsed_type == "episode" else parsed_type
    return 1.0 if parsed_norm == tmdb_type else 0.0


def _popularity_score(popularity: float) -> float:
    """Normalize TMDB popularity to 0-1 using log scale.

    TMDB popularity ranges from ~0 to ~1000+. Log scaling gives
    reasonable distribution. Clamped to [0, 1].
    """
    if popularity <= 0:
        return 0.0
    # log10(1000) ≈ 3, so dividing by 3 normalizes to ~0-1
    return min(1.0, math.log10(popularity + 1) / 3.0)
