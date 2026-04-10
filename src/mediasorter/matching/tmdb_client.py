"""TMDB API client with caching and rate limiting.

Uses tmdbv3api for API access. All responses are cached in SQLite
with a configurable TTL to avoid hitting TMDB's rate limits (40 req/10s).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlmodel import Session, select
from tmdbv3api import Movie, TMDb, TV, Season as TMDBSeason

from mediasorter.config import TMDBConfig
from mediasorter.db.models import TMDBCache
from mediasorter.parsing.guessit_wrapper import ParsedMedia
from mediasorter.parsing.normalize import normalize_for_comparison
from mediasorter.utils.rate_limit import TokenBucket

log = structlog.get_logger(__name__)


@dataclass
class TMDBResult:
    """Normalized TMDB search result."""

    tmdb_id: int
    imdb_id: str | None
    title: str
    original_title: str
    year: int | None
    media_type: str  # "movie" | "tv"
    overview: str
    popularity: float


class TMDBClient:
    """TMDB API client with caching and rate limiting."""

    def __init__(self, config: TMDBConfig, rate_limiter: TokenBucket | None = None):
        self._tmdb = TMDb()
        self._tmdb.api_key = config.api_key
        self._tmdb.language = config.language
        self._movie = Movie()
        self._tv = TV()
        self._season = TMDBSeason()
        self._limiter = rate_limiter or TokenBucket()
        self._config = config

    def search_movie(self, title: str, year: int | None = None) -> list[TMDBResult]:
        """Search TMDB for movies. Includes year in query for better results."""
        self._limiter.acquire()
        try:
            # tmdbv3api Movie.search only accepts (term, page)
            # Append year to search term for better precision
            query = f"{title} {year}" if year else title
            results = self._movie.search(query)
            items = self._convert_results(results, "movie")

            # If year-appended search returned nothing, retry without year
            if not items and year:
                self._limiter.acquire()
                results = self._movie.search(title)
                items = self._convert_results(results, "movie")

            return items
        except Exception as e:
            log.warning("tmdb_search_failed", title=title, year=year, error=str(e))
            return []

    def search_tv(self, title: str, year: int | None = None) -> list[TMDBResult]:
        """Search TMDB for TV shows. Includes year in query for disambiguation."""
        self._limiter.acquire()
        try:
            # tmdbv3api TV.search only accepts (term, page)
            query = f"{title} {year}" if year else title
            results = self._tv.search(query)
            items = self._convert_results(results, "tv")

            if not items and year:
                self._limiter.acquire()
                results = self._tv.search(title)
                items = self._convert_results(results, "tv")

            return items
        except Exception as e:
            log.warning("tmdb_search_failed", title=title, year=year, error=str(e))
            return []

    def search(self, parsed: ParsedMedia) -> list[TMDBResult]:
        """Search TMDB based on parsed media type."""
        title = parsed.title
        year = parsed.year

        if parsed.media_type == "movie":
            return self.search_movie(title, year)
        else:
            return self.search_tv(title, year)

    def get_tv_episode(self, tv_id: int, season: int, episode: int) -> dict | None:
        """Get episode details (title, air date) for canonical naming."""
        self._limiter.acquire()
        try:
            ep = self._season.details(tv_id, season)
            for ep_data in getattr(ep, "episodes", []):
                if getattr(ep_data, "episode_number", None) == episode:
                    return {
                        "name": getattr(ep_data, "name", ""),
                        "air_date": getattr(ep_data, "air_date", ""),
                        "episode_number": episode,
                        "season_number": season,
                    }
            return None
        except Exception as e:
            log.warning("tmdb_episode_fetch_failed", tv_id=tv_id, season=season, episode=episode, error=str(e))
            return None

    def get_movie_details(self, movie_id: int) -> dict | None:
        """Get movie details (imdb_id, original title)."""
        self._limiter.acquire()
        try:
            details = self._movie.details(movie_id)
            return {
                "imdb_id": getattr(details, "imdb_id", None),
                "original_title": getattr(details, "original_title", ""),
                "title": getattr(details, "title", ""),
                "release_date": getattr(details, "release_date", ""),
            }
        except Exception as e:
            log.warning("tmdb_movie_details_failed", movie_id=movie_id, error=str(e))
            return None

    def get_tv_details(self, tv_id: int) -> dict | None:
        """Get TV show details (for next_episode_to_air)."""
        self._limiter.acquire()
        try:
            details = self._tv.details(tv_id)
            next_ep = getattr(details, "next_episode_to_air", None)
            return {
                "name": getattr(details, "name", ""),
                "first_air_date": getattr(details, "first_air_date", ""),
                "next_episode_to_air": dict(next_ep) if next_ep else None,
                "external_ids": getattr(details, "external_ids", {}),
            }
        except Exception as e:
            log.warning("tmdb_tv_details_failed", tv_id=tv_id, error=str(e))
            return None

    def _convert_results(self, results, media_type: str) -> list[TMDBResult]:
        """Convert tmdbv3api results to TMDBResult dataclasses."""
        items = []
        for r in results:
            try:
                if media_type == "movie":
                    title = getattr(r, "title", "")
                    original_title = getattr(r, "original_title", title)
                    release_date = getattr(r, "release_date", "") or ""
                    year = int(release_date[:4]) if len(release_date) >= 4 else None
                else:
                    title = getattr(r, "name", "")
                    original_title = getattr(r, "original_name", title)
                    first_air = getattr(r, "first_air_date", "") or ""
                    year = int(first_air[:4]) if len(first_air) >= 4 else None

                items.append(TMDBResult(
                    tmdb_id=int(getattr(r, "id", 0)),
                    imdb_id=None,  # not available in search results
                    title=title,
                    original_title=original_title,
                    year=year,
                    media_type=media_type,
                    overview=getattr(r, "overview", "") or "",
                    popularity=float(getattr(r, "popularity", 0)),
                ))
            except Exception as e:
                log.debug("tmdb_result_parse_error", error=str(e))
                continue

        return items


class CachedTMDBClient:
    """Wrapper around TMDBClient that caches search results in SQLite."""

    def __init__(self, client: TMDBClient, engine, ttl_days: int = 30):
        self._client = client
        self._engine = engine
        self._ttl = timedelta(days=ttl_days)

    def search(self, parsed: ParsedMedia) -> list[TMDBResult]:
        """Search with cache layer."""
        key = self._cache_key(parsed)
        cached = self._get_cached(key)
        if cached is not None:
            log.debug("tmdb_cache_hit", key=key)
            return cached

        log.debug("tmdb_cache_miss", key=key)
        results = self._client.search(parsed)
        self._store_cached(key, results)
        return results

    def get_tv_episode(self, tv_id: int, season: int, episode: int) -> dict | None:
        """Delegate to client (episode details not cached)."""
        return self._client.get_tv_episode(tv_id, season, episode)

    def get_movie_details(self, movie_id: int) -> dict | None:
        """Delegate to client."""
        return self._client.get_movie_details(movie_id)

    def get_tv_details(self, tv_id: int) -> dict | None:
        """Delegate to client."""
        return self._client.get_tv_details(tv_id)

    def invalidate(self, parsed: ParsedMedia) -> None:
        """Remove a cache entry."""
        key = self._cache_key(parsed)
        with Session(self._engine) as session:
            stmt = select(TMDBCache).where(TMDBCache.cache_key == key)
            entry = session.exec(stmt).first()
            if entry:
                session.delete(entry)
                session.commit()

    def _cache_key(self, parsed: ParsedMedia) -> str:
        title_norm = normalize_for_comparison(parsed.title)
        return f"{parsed.media_type}:{title_norm}:{parsed.year or 'none'}"

    def _get_cached(self, key: str) -> list[TMDBResult] | None:
        with Session(self._engine) as session:
            stmt = select(TMDBCache).where(TMDBCache.cache_key == key)
            entry = session.exec(stmt).first()
            if entry is None:
                return None

            if datetime.now(tz=None) - entry.fetched_at > self._ttl:
                session.delete(entry)
                session.commit()
                return None

            try:
                data = json.loads(entry.response_json)
                return [TMDBResult(**item) for item in data]
            except (json.JSONDecodeError, TypeError):
                return None

    def _store_cached(self, key: str, results: list[TMDBResult]) -> None:
        data = json.dumps([
            {
                "tmdb_id": r.tmdb_id,
                "imdb_id": r.imdb_id,
                "title": r.title,
                "original_title": r.original_title,
                "year": r.year,
                "media_type": r.media_type,
                "overview": r.overview,
                "popularity": r.popularity,
            }
            for r in results
        ])

        with Session(self._engine) as session:
            # Upsert: delete old, insert new
            stmt = select(TMDBCache).where(TMDBCache.cache_key == key)
            existing = session.exec(stmt).first()
            if existing:
                existing.response_json = data
                existing.fetched_at = datetime.now(tz=None)
                session.add(existing)
            else:
                entry = TMDBCache(
                    cache_key=key,
                    response_json=data,
                    fetched_at=datetime.now(tz=None),
                )
                session.add(entry)
            session.commit()
