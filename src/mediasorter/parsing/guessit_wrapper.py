"""GuessIt wrapper for parsing media filenames.

Wraps the guessit library to produce a normalized ParsedMedia dataclass.
Handles edge cases: anime absolute numbering, multi-episode files,
parent folder context for ambiguous filenames.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import guessit as guessit_lib

from mediasorter.utils.fs import VIDEO_EXTENSIONS


@dataclass
class ParsedMedia:
    """Normalized parse result from a media filename."""

    title: str
    year: int | None = None
    media_type: Literal["movie", "episode"] = "episode"
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    episode_title: str | None = None
    absolute_episode: int | None = None
    source: str | None = None  # BluRay, HDTV, WEB-DL
    resolution: str | None = None  # 720p, 1080p, 2160p
    release_group: str | None = None
    container: str | None = None  # mkv, mp4
    raw: dict = field(default_factory=dict)


def parse_filename(filepath: str | Path) -> ParsedMedia:
    """Parse a media filename using guessit and return normalized ParsedMedia.

    Uses both the filename and parent folder for context. Handles:
    - Standard SxxExx format
    - Anime absolute numbering
    - Multi-episode files (S01E01E02, S01E01-E02)
    - Movies with year
    """
    filepath = Path(filepath)
    filename = filepath.name

    # Try parsing with the full path for better context
    # guessit uses parent folder names to disambiguate
    try:
        raw = dict(guessit_lib.guessit(str(filepath)))
    except Exception:
        raw = dict(guessit_lib.guessit(filename))

    title = str(raw.get("title", filepath.stem))
    year = raw.get("year")
    media_type = raw.get("type", "episode")

    # Normalize media_type
    if media_type not in ("movie", "episode"):
        media_type = _infer_type_from_context(filepath)

    # Extract season
    season = raw.get("season")

    # Extract episode(s) — normalize to list
    episodes = _extract_episodes(raw)

    # Absolute episode (anime)
    absolute_episode = raw.get("episode", None) if "season" not in raw and not episodes else None
    if isinstance(absolute_episode, list):
        absolute_episode = absolute_episode[0] if absolute_episode else None

    # For anime: if absolute episode but no season, default season=1
    if absolute_episode is not None and season is None and media_type == "episode":
        season = 1
        if not episodes:
            episodes = [absolute_episode]

    # Extract episode title
    episode_title = raw.get("episode_title")
    if isinstance(episode_title, list):
        episode_title = " ".join(str(e) for e in episode_title)

    return ParsedMedia(
        title=title,
        year=int(year) if year is not None else None,
        media_type=media_type,
        season=int(season) if season is not None else None,
        episodes=episodes,
        episode_title=str(episode_title) if episode_title else None,
        absolute_episode=int(absolute_episode) if absolute_episode is not None else None,
        source=str(raw.get("source", "")) or None,
        resolution=str(raw.get("screen_size", "")) or None,
        release_group=str(raw.get("release_group", "")) or None,
        container=filepath.suffix.lstrip(".").lower() or None,
        raw=raw,
    )


def _extract_episodes(raw: dict) -> list[int]:
    """Extract episode numbers from guessit output, always as a list."""
    ep = raw.get("episode")
    if ep is None:
        return []
    if isinstance(ep, list):
        return [int(e) for e in ep]
    return [int(ep)]


def _infer_type_from_context(filepath: Path) -> Literal["movie", "episode"]:
    """Infer media type from parent folder names when guessit can't determine it."""
    parts_lower = [p.lower() for p in filepath.parts]
    movie_hints = {"movie", "movies", "film", "films"}
    tv_hints = {"tv", "shows", "series", "anime", "season"}

    for part in parts_lower:
        if any(h in part for h in movie_hints):
            return "movie"
        if any(h in part for h in tv_hints):
            return "episode"

    return "episode"  # default to episode (more common in messy libraries)
