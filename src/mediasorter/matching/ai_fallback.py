"""OpenRouter AI fallback for low-confidence matches.

When TMDB matching fails or confidence is below threshold, this module
sends the filename context to an AI model via OpenRouter to identify
the media. Results are validated with pydantic and cached.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

from mediasorter.config import OpenRouterConfig
from mediasorter.db.models import TMDBCache
from mediasorter.matching.tmdb_client import TMDBResult
from mediasorter.parsing.guessit_wrapper import ParsedMedia

log = structlog.get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are a media identification expert. Given a filename and context about a media file, identify what movie or TV show it is.

You MUST respond with ONLY a JSON object in this exact format:
{
  "tmdb_id": <int or null>,
  "media_type": "movie" or "tv",
  "title": "<canonical title>",
  "year": <int>,
  "season": <int or null>,
  "episode": <int or null>,
  "confidence": <float 0.0-1.0>,
  "reasoning": "<brief explanation>"
}

If you cannot identify the media, set confidence to 0 and tmdb_id to null."""


class AIIdentification(BaseModel):
    """Validated AI response."""

    tmdb_id: int | None = None
    media_type: str = "tv"
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    confidence: float = 0.0
    reasoning: str = ""


@dataclass
class AICallStats:
    """Tracking for AI API usage."""

    tokens_used: int = 0
    estimated_cost: float = 0.0


class AIFallback:
    """OpenRouter AI fallback for media identification."""

    def __init__(self, config: OpenRouterConfig, engine=None):
        self._config = config
        self._engine = engine
        self._session_cost = 0.0
        self._client = httpx.Client(timeout=30)

    def identify(
        self,
        filepath: Path,
        parsed: ParsedMedia,
        tmdb_candidates: list[TMDBResult],
        sibling_names: list[str] | None = None,
    ) -> tuple[TMDBResult | None, AICallStats]:
        """Attempt to identify media via AI.

        Returns (TMDBResult or None, call stats).
        """
        if not self._config.enabled or not self._config.api_key:
            return None, AICallStats()

        # Check budget
        if self._session_cost >= self._config.max_cost_per_run_usd:
            log.warning("ai_budget_exceeded", cost=self._session_cost)
            return None, AICallStats()

        # Check cache
        cache_key = self._cache_key(filepath)
        cached = self._get_cached(cache_key)
        if cached is not None:
            log.debug("ai_cache_hit", key=cache_key)
            return cached, AICallStats()

        # Build prompt
        user_prompt = self._build_prompt(filepath, parsed, tmdb_candidates, sibling_names)

        # Call OpenRouter
        result, stats = self._call_api(user_prompt)
        if result is None:
            return None, stats

        # Validate and convert
        tmdb_result = self._to_tmdb_result(result)

        # Cache result
        if tmdb_result and self._engine:
            self._store_cached(cache_key, tmdb_result)

        self._session_cost += stats.estimated_cost
        return tmdb_result, stats

    def _build_prompt(
        self,
        filepath: Path,
        parsed: ParsedMedia,
        tmdb_candidates: list[TMDBResult],
        sibling_names: list[str] | None,
    ) -> str:
        """Build the user prompt with all available context."""
        parts = [
            f"Filename: {filepath.name}",
            f"Full path: {filepath}",
            f"Parent folder: {filepath.parent.name}",
        ]

        if sibling_names:
            parts.append(f"Sibling files: {', '.join(sibling_names[:3])}")

        parts.append(f"\nGuessIt parsed output:")
        parts.append(f"  Title: {parsed.title}")
        parts.append(f"  Year: {parsed.year}")
        parts.append(f"  Type: {parsed.media_type}")
        parts.append(f"  Season: {parsed.season}")
        parts.append(f"  Episodes: {parsed.episodes}")

        if tmdb_candidates:
            parts.append(f"\nTop TMDB candidates:")
            for i, c in enumerate(tmdb_candidates[:5], 1):
                parts.append(
                    f"  {i}. {c.title} ({c.year}) [tmdb_id={c.tmdb_id}, "
                    f"type={c.media_type}, popularity={c.popularity:.1f}]"
                )
                if c.overview:
                    parts.append(f"     {c.overview[:100]}...")

        parts.append("\nIdentify this media. If one of the TMDB candidates is correct, use its tmdb_id.")

        return "\n".join(parts)

    def _call_api(self, user_prompt: str) -> tuple[AIIdentification | None, AICallStats]:
        """Make the OpenRouter API call."""
        stats = AICallStats()

        try:
            response = self._client.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._config.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
            )
            response.raise_for_status()
            data = response.json()

            # Extract usage stats
            usage = data.get("usage", {})
            stats.tokens_used = usage.get("total_tokens", 0)
            # Rough cost estimate (varies by model)
            stats.estimated_cost = stats.tokens_used * 0.000001  # ~$1/M tokens

            # Parse response content
            content = data["choices"][0]["message"]["content"]
            log.info(
                "ai_call_complete",
                model=self._config.model,
                tokens=stats.tokens_used,
                cost=f"${stats.estimated_cost:.6f}",
            )

            # Try to parse JSON from response
            result = self._parse_response(content)
            return result, stats

        except httpx.HTTPStatusError as e:
            log.error("ai_api_error", status=e.response.status_code, error=str(e))
            return None, stats
        except Exception as e:
            log.error("ai_call_failed", error=str(e))
            return None, stats

    def _parse_response(self, content: str) -> AIIdentification | None:
        """Parse and validate the AI response JSON."""
        # Try to extract JSON from the response (AI might wrap it in markdown)
        json_str = content.strip()
        if "```" in json_str:
            # Extract from code block
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = json_str[start:end]

        try:
            data = json.loads(json_str)
            return AIIdentification.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            log.warning("ai_response_parse_failed", error=str(e), content=content[:200])
            return None

    def _to_tmdb_result(self, ai: AIIdentification) -> TMDBResult | None:
        """Convert AI identification to TMDBResult."""
        if ai.confidence < 0.3 or not ai.title:
            return None

        return TMDBResult(
            tmdb_id=ai.tmdb_id or 0,
            imdb_id=None,
            title=ai.title,
            original_title=ai.title,
            year=ai.year,
            media_type=ai.media_type,
            overview=f"AI identified: {ai.reasoning}",
            popularity=0,
        )

    def _cache_key(self, filepath: Path) -> str:
        """Generate cache key from filename + parent folder."""
        key = f"{filepath.parent.name}/{filepath.name}"
        return f"ai:{hashlib.md5(key.encode()).hexdigest()}"

    def _get_cached(self, key: str) -> TMDBResult | None:
        """Check SQLite cache for AI result."""
        if not self._engine:
            return None

        with Session(self._engine) as session:
            entry = session.exec(
                select(TMDBCache).where(TMDBCache.cache_key == key)
            ).first()
            if entry is None:
                return None

            try:
                data = json.loads(entry.response_json)
                return TMDBResult(**data)
            except (json.JSONDecodeError, TypeError):
                return None

    def _store_cached(self, key: str, result: TMDBResult) -> None:
        """Store AI result in SQLite cache."""
        data = json.dumps({
            "tmdb_id": result.tmdb_id,
            "imdb_id": result.imdb_id,
            "title": result.title,
            "original_title": result.original_title,
            "year": result.year,
            "media_type": result.media_type,
            "overview": result.overview,
            "popularity": result.popularity,
        })

        with Session(self._engine) as session:
            existing = session.exec(
                select(TMDBCache).where(TMDBCache.cache_key == key)
            ).first()
            if existing:
                existing.response_json = data
                existing.fetched_at = datetime.now(tz=None)
                session.add(existing)
            else:
                session.add(TMDBCache(
                    cache_key=key,
                    response_json=data,
                    fetched_at=datetime.now(tz=None),
                ))
            session.commit()
