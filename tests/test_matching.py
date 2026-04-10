"""Tests for TMDB matching: client, cache, and confidence scorer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mediasorter.db.engine import create_tables, get_engine
from mediasorter.matching.scorer import (
    best_match,
    score_match,
)
from mediasorter.matching.tmdb_client import (
    CachedTMDBClient,
    TMDBClient,
    TMDBResult,
)
from mediasorter.parsing.guessit_wrapper import ParsedMedia


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_parsed(title="Breaking Bad", year=None, media_type="episode", season=1, episodes=None):
    return ParsedMedia(
        title=title,
        year=year,
        media_type=media_type,
        season=season,
        episodes=episodes or [1],
        raw={},
    )


def make_result(
    tmdb_id=1396,
    title="Breaking Bad",
    original_title="Breaking Bad",
    year=2008,
    media_type="tv",
    popularity=200.0,
):
    return TMDBResult(
        tmdb_id=tmdb_id,
        imdb_id=None,
        title=title,
        original_title=original_title,
        year=year,
        media_type=media_type,
        overview="A chemistry teacher turned drug lord.",
        popularity=popularity,
    )


@pytest.fixture
def db_engine(tmp_path):
    engine = get_engine(tmp_path / "test.db")
    create_tables(engine)
    return engine


# ---------------------------------------------------------------------------
# Scorer tests
# ---------------------------------------------------------------------------


class TestScoreMatch:
    def test_exact_match_high_confidence(self):
        parsed = make_parsed(title="Breaking Bad", year=2008, media_type="episode")
        result = make_result(title="Breaking Bad", year=2008, media_type="tv", popularity=200)
        score = score_match(parsed, result)
        assert score >= 0.85

    def test_wrong_title_low_confidence(self):
        parsed = make_parsed(title="Breaking Bad", year=2008)
        result = make_result(
            title="Stranger Things", original_title="Stranger Things",
            year=2016, media_type="tv",
        )
        score = score_match(parsed, result)
        assert score < 0.5

    def test_year_off_by_one_still_reasonable(self):
        parsed = make_parsed(title="The Matrix", year=1999, media_type="movie")
        result = make_result(title="The Matrix", year=1999, media_type="movie", tmdb_id=603)
        score_exact = score_match(parsed, result)

        result_off = make_result(title="The Matrix", year=2000, media_type="movie", tmdb_id=603)
        score_off = score_match(parsed, result_off)

        assert score_exact > score_off
        assert score_off > 0.5  # still reasonable

    def test_no_year_in_parsed(self):
        parsed = make_parsed(title="Friends", year=None, media_type="episode")
        result = make_result(title="Friends", year=1994, media_type="tv")
        score = score_match(parsed, result)
        assert 0.5 < score < 1.0  # partial credit for missing year

    def test_type_mismatch_penalized(self):
        parsed = make_parsed(title="The Matrix", year=1999, media_type="movie")
        result_match = make_result(title="The Matrix", year=1999, media_type="movie")
        result_mismatch = make_result(title="The Matrix", year=1999, media_type="tv")

        score_match_val = score_match(parsed, result_match)
        score_mismatch_val = score_match(parsed, result_mismatch)

        assert score_match_val > score_mismatch_val

    def test_popularity_boost(self):
        parsed = make_parsed(title="Friends", media_type="episode")
        popular = make_result(title="Friends", media_type="tv", popularity=500)
        obscure = make_result(title="Friends", media_type="tv", popularity=1, tmdb_id=99999)

        assert score_match(parsed, popular) > score_match(parsed, obscure)

    def test_score_range(self):
        """Score should always be in [0, 1]."""
        parsed = make_parsed(title="XYZ", year=2020, media_type="movie")
        result = make_result(title="ABC", year=1950, media_type="tv", popularity=0)
        score = score_match(parsed, result)
        assert 0.0 <= score <= 1.0


class TestBestMatch:
    def test_returns_best_above_threshold(self):
        parsed = make_parsed(title="Breaking Bad", year=2008, media_type="episode")
        results = [
            make_result(title="Breaking Bad", year=2008, media_type="tv"),
            make_result(title="Bad Breaking", year=2010, media_type="tv", tmdb_id=9999),
        ]
        match, score = best_match(parsed, results, threshold=0.5)
        assert match is not None
        assert match.tmdb_id == 1396
        assert score >= 0.5

    def test_returns_none_below_threshold(self):
        parsed = make_parsed(title="XYZ Nonexistent Show", year=2020)
        results = [
            make_result(title="Completely Different", year=1990, media_type="movie"),
        ]
        match, score = best_match(parsed, results, threshold=0.85)
        assert match is None

    def test_empty_results(self):
        parsed = make_parsed(title="Anything")
        match, score = best_match(parsed, [], threshold=0.5)
        assert match is None
        assert score == 0.0


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------


class TestCachedTMDBClient:
    def test_cache_miss_calls_client(self, db_engine):
        mock_client = MagicMock(spec=TMDBClient)
        mock_client.search.return_value = [make_result()]

        cached = CachedTMDBClient(mock_client, db_engine, ttl_days=30)
        parsed = make_parsed(title="Breaking Bad")

        results = cached.search(parsed)
        assert len(results) == 1
        mock_client.search.assert_called_once()

    def test_cache_hit_skips_client(self, db_engine):
        mock_client = MagicMock(spec=TMDBClient)
        mock_client.search.return_value = [make_result()]

        cached = CachedTMDBClient(mock_client, db_engine, ttl_days=30)
        parsed = make_parsed(title="Breaking Bad")

        # First call: cache miss
        cached.search(parsed)
        # Second call: cache hit
        results = cached.search(parsed)

        assert len(results) == 1
        assert mock_client.search.call_count == 1  # only called once

    def test_cache_invalidate(self, db_engine):
        mock_client = MagicMock(spec=TMDBClient)
        mock_client.search.return_value = [make_result()]

        cached = CachedTMDBClient(mock_client, db_engine, ttl_days=30)
        parsed = make_parsed(title="Breaking Bad")

        cached.search(parsed)
        cached.invalidate(parsed)
        cached.search(parsed)

        assert mock_client.search.call_count == 2  # called again after invalidation

    def test_delegates_episode_details(self, db_engine):
        mock_client = MagicMock(spec=TMDBClient)
        mock_client.get_tv_episode.return_value = {"name": "Pilot"}

        cached = CachedTMDBClient(mock_client, db_engine)
        result = cached.get_tv_episode(1396, 1, 1)

        assert result == {"name": "Pilot"}
        mock_client.get_tv_episode.assert_called_once_with(1396, 1, 1)


# ---------------------------------------------------------------------------
# TMDBClient unit tests (mocked tmdbv3api)
# ---------------------------------------------------------------------------


class TestTMDBClient:
    @patch("mediasorter.matching.tmdb_client.Movie")
    @patch("mediasorter.matching.tmdb_client.TV")
    @patch("mediasorter.matching.tmdb_client.TMDb")
    def test_search_movie(self, mock_tmdb_cls, mock_tv_cls, mock_movie_cls):
        from mediasorter.config import TMDBConfig

        mock_movie = MagicMock()
        mock_result = MagicMock()
        mock_result.id = 603
        mock_result.title = "The Matrix"
        mock_result.original_title = "The Matrix"
        mock_result.release_date = "1999-03-31"
        mock_result.overview = "A computer hacker..."
        mock_result.popularity = 100.0
        mock_movie.search.return_value = [mock_result]
        mock_movie_cls.return_value = mock_movie

        config = TMDBConfig(api_key="fake_key")
        client = TMDBClient(config)
        client._movie = mock_movie

        parsed = make_parsed(title="The Matrix", year=1999, media_type="movie")
        results = client.search(parsed)

        assert len(results) == 1
        assert results[0].tmdb_id == 603
        assert results[0].title == "The Matrix"
        assert results[0].year == 1999

    @patch("mediasorter.matching.tmdb_client.TV")
    @patch("mediasorter.matching.tmdb_client.Movie")
    @patch("mediasorter.matching.tmdb_client.TMDb")
    def test_search_tv(self, mock_tmdb_cls, mock_movie_cls, mock_tv_cls):
        from mediasorter.config import TMDBConfig

        mock_tv = MagicMock()
        mock_result = MagicMock()
        mock_result.id = 1396
        mock_result.name = "Breaking Bad"
        mock_result.original_name = "Breaking Bad"
        mock_result.first_air_date = "2008-01-20"
        mock_result.overview = "A chemistry teacher..."
        mock_result.popularity = 200.0
        mock_tv.search.return_value = [mock_result]
        mock_tv_cls.return_value = mock_tv

        config = TMDBConfig(api_key="fake_key")
        client = TMDBClient(config)
        client._tv = mock_tv

        parsed = make_parsed(title="Breaking Bad", media_type="episode")
        results = client.search(parsed)

        assert len(results) == 1
        assert results[0].tmdb_id == 1396
        assert results[0].media_type == "tv"

    @patch("mediasorter.matching.tmdb_client.Movie")
    @patch("mediasorter.matching.tmdb_client.TV")
    @patch("mediasorter.matching.tmdb_client.TMDb")
    def test_search_fallback_without_year(self, mock_tmdb_cls, mock_tv_cls, mock_movie_cls):
        from mediasorter.config import TMDBConfig

        mock_movie = MagicMock()
        # First call with year returns empty
        # Second call without year returns result
        mock_result = MagicMock()
        mock_result.id = 603
        mock_result.title = "The Matrix"
        mock_result.original_title = "The Matrix"
        mock_result.release_date = "1999-03-31"
        mock_result.overview = ""
        mock_result.popularity = 100.0
        mock_movie.search.side_effect = [[], [mock_result]]
        mock_movie_cls.return_value = mock_movie

        config = TMDBConfig(api_key="fake_key")
        client = TMDBClient(config)
        client._movie = mock_movie

        results = client.search_movie("The Matrix", year=2000)

        assert len(results) == 1
        assert mock_movie.search.call_count == 2
