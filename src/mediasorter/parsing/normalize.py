"""Filename sanitization and normalization.

Handles:
- Removing/replacing filesystem-forbidden characters
- Normalizing titles for TMDB search (stripping noise)
- Formatting episode codes (S01E01, S01E01-E02)
"""

from __future__ import annotations

import re
import unicodedata

# Characters forbidden on common filesystems (NTFS, CIFS, ext4 safe subset)
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*]')

# Noise patterns to strip for search queries
_NOISE_PATTERNS = re.compile(
    r"\b(720p|1080p|2160p|4k|uhd|hdr|bluray|blu-ray|bdrip|brrip|dvdrip|"
    r"webrip|web-dl|webdl|hdtv|pdtv|sdtv|hdrip|remux|proper|repack|"
    r"x264|x265|h264|h265|hevc|aac|ac3|dts|mp3|flac|"
    r"extended|unrated|directors\.cut|theatrical|imax|"
    r"multi|dual|subbed|dubbed)\b",
    re.IGNORECASE,
)

# Release group in brackets at end: [GroupName] or -GroupName
_RELEASE_GROUP = re.compile(r"\s*[-\[].+?[\]]*$")

# Multiple spaces/dots/underscores -> single space
_SEPARATORS = re.compile(r"[._]+")
_MULTI_SPACE = re.compile(r"\s{2,}")


def sanitize_filename(name: str) -> str:
    """Remove/replace characters forbidden by filesystems.

    Preserves unicode characters. Replaces forbidden chars with hyphen,
    then collapses multiple hyphens.
    """
    result = _FORBIDDEN_CHARS.sub("-", name)
    result = re.sub(r"-{2,}", "-", result)
    result = result.strip("- ")
    return result


def normalize_for_search(title: str) -> str:
    """Prepare a title for TMDB search.

    Strips quality tags, release groups, and normalizes separators
    to produce a clean search query.
    """
    result = _SEPARATORS.sub(" ", title)
    result = _NOISE_PATTERNS.sub("", result)
    result = _RELEASE_GROUP.sub("", result)
    result = _MULTI_SPACE.sub(" ", result)
    result = result.strip()
    return result


def normalize_for_comparison(title: str) -> str:
    """Normalize a title for string comparison.

    Lowercases, removes accents (for comparison only, not display),
    strips leading articles, and removes punctuation.
    """
    result = title.lower()
    # Decompose unicode and remove combining chars (accents)
    result = unicodedata.normalize("NFD", result)
    result = "".join(c for c in result if unicodedata.category(c) != "Mn")
    # Strip leading articles
    result = re.sub(r"^(the|a|an)\s+", "", result)
    # Remove punctuation
    result = re.sub(r"[^\w\s]", "", result)
    result = _MULTI_SPACE.sub(" ", result).strip()
    return result


def format_episode_code(season: int, episodes: list[int]) -> str:
    """Format a season/episode code for filenames.

    Examples:
        format_episode_code(1, [1]) -> "S01E01"
        format_episode_code(1, [1, 2]) -> "S01E01-E02"
        format_episode_code(3, [9, 10]) -> "S03E09-E10"
    """
    season_str = f"S{season:02d}"
    if not episodes:
        return season_str

    ep_parts = [f"E{episodes[0]:02d}"]
    for ep in episodes[1:]:
        ep_parts.append(f"E{ep:02d}")

    return season_str + "-".join(ep_parts)
