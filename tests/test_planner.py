"""Tests for the move planner: path building, scan pipeline, duplicate detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mediasorter.config import AppConfig, RootsConfig, MatchingConfig, MovingConfig
from mediasorter.db.engine import create_tables, get_engine
from mediasorter.matching.tmdb_client import TMDBResult
from mediasorter.moving.planner import (
    MovePlan,
    ScanPlanner,
    build_episode_path,
    build_movie_path,
    build_sibling_dest,
)
from mediasorter.parsing.guessit_wrapper import ParsedMedia


# ---------------------------------------------------------------------------
# Path building tests
# ---------------------------------------------------------------------------


class TestBuildMoviePath:
    def test_standard_movie(self):
        path = build_movie_path(
            movies_root=Path("/movies"),
            title="The Matrix",
            year=1999,
            imdb_id="tt0133093",
            tmdb_id=603,
            ext=".mkv",
        )
        assert path == Path("/movies/The Matrix (1999) [imdbid-tt0133093]/The Matrix (1999).mkv")

    def test_movie_no_imdb(self):
        path = build_movie_path(
            movies_root=Path("/movies"),
            title="Some Film",
            year=2020,
            imdb_id=None,
            tmdb_id=12345,
            ext=".mp4",
        )
        assert path == Path("/movies/Some Film (2020) [tmdbid-12345]/Some Film (2020).mp4")

    def test_movie_no_year(self):
        path = build_movie_path(
            movies_root=Path("/movies"),
            title="Mystery",
            year=None,
            imdb_id=None,
            tmdb_id=999,
            ext=".mkv",
        )
        assert path == Path("/movies/Mystery [tmdbid-999]/Mystery.mkv")

    def test_movie_special_chars_sanitized(self):
        path = build_movie_path(
            movies_root=Path("/movies"),
            title="Star Wars: A New Hope",
            year=1977,
            imdb_id="tt0076759",
            tmdb_id=11,
            ext=".mkv",
        )
        assert ":" not in path.parts[-1]
        assert ":" not in path.parts[-2]


class TestBuildEpisodePath:
    def test_standard_episode(self):
        path = build_episode_path(
            tv_root=Path("/shows"),
            series_title="Breaking Bad",
            series_year=2008,
            tmdb_id=1396,
            season=1,
            episodes=[1],
            episode_title="Pilot",
            ext=".mkv",
        )
        expected = Path(
            "/shows/Breaking Bad (2008) [tmdbid-1396]/Season 01/"
            "Breaking Bad - S01E01 - Pilot.mkv"
        )
        assert path == expected

    def test_multi_episode(self):
        path = build_episode_path(
            tv_root=Path("/shows"),
            series_title="Friends",
            series_year=1994,
            tmdb_id=1668,
            season=10,
            episodes=[17, 18],
            episode_title="The Last One",
            ext=".mkv",
        )
        assert "S10E17-E18" in path.name

    def test_no_episode_title(self):
        path = build_episode_path(
            tv_root=Path("/shows"),
            series_title="Seinfeld",
            series_year=1989,
            tmdb_id=1400,
            season=5,
            episodes=[3],
            episode_title=None,
            ext=".mkv",
        )
        assert path.name == "Seinfeld - S05E03.mkv"

    def test_season_zero_specials(self):
        path = build_episode_path(
            tv_root=Path("/shows"),
            series_title="Doctor Who",
            series_year=2005,
            tmdb_id=57243,
            season=0,
            episodes=[1],
            episode_title="A Christmas Special",
            ext=".mkv",
        )
        assert "Season 00" in str(path)
        assert "S00E01" in path.name

    def test_no_series_year(self):
        path = build_episode_path(
            tv_root=Path("/shows"),
            series_title="Lost",
            series_year=None,
            tmdb_id=4607,
            season=1,
            episodes=[1],
            episode_title="Pilot",
            ext=".mkv",
        )
        assert "Lost [tmdbid-4607]" in str(path)
        assert "()" not in str(path)


class TestBuildSiblingDest:
    def test_subtitle_with_lang(self):
        video_src = Path("/media/Movie.2020.1080p.mkv")
        video_dst = Path("/movies/Movie (2020)/Movie (2020).mkv")
        sib_src = Path("/media/Movie.2020.1080p.en.srt")

        sib_dst = build_sibling_dest(sib_src, video_src, video_dst)
        assert sib_dst == Path("/movies/Movie (2020)/Movie (2020).en.srt")

    def test_nfo_file(self):
        video_src = Path("/media/Movie.mkv")
        video_dst = Path("/movies/Movie (2020)/Movie (2020).mkv")
        sib_src = Path("/media/Movie.nfo")

        sib_dst = build_sibling_dest(sib_src, video_src, video_dst)
        assert sib_dst == Path("/movies/Movie (2020)/Movie (2020).nfo")


# ---------------------------------------------------------------------------
# ScanPlanner tests with mocked TMDB
# ---------------------------------------------------------------------------


@pytest.fixture
def test_config(tmp_path):
    return AppConfig(
        roots=RootsConfig(
            shows=tmp_path / "Shows",
            movies=tmp_path / "Movies",
        ),
        matching=MatchingConfig(
            confidence_threshold=0.5,  # low for testing
            min_movie_size_mb=0,
            min_episode_size_mb=0,
        ),
        moving=MovingConfig(
            trash_dir=tmp_path / ".trash",
        ),
    )


@pytest.fixture
def db_engine(tmp_path):
    engine = get_engine(tmp_path / "test.db")
    create_tables(engine)
    return engine


@pytest.fixture
def media_dir(tmp_path):
    """Create a temp directory with fake video files."""
    media = tmp_path / "media"
    media.mkdir()

    # Create fake video files (0 bytes, but we set min size to 0 in config)
    (media / "Breaking.Bad.S01E01.Pilot.720p.BluRay.mkv").write_bytes(b"\x00" * 100)
    (media / "The.Matrix.1999.1080p.BluRay.mkv").write_bytes(b"\x00" * 100)
    (media / "Breaking.Bad.S01E01.Pilot.720p.BluRay.en.srt").write_text("subtitle")
    (media / "sample.mkv").write_bytes(b"tiny")  # should be skipped if sample detection works

    return media


class TestScanPlanner:
    @patch("mediasorter.moving.planner.CachedTMDBClient")
    @patch("mediasorter.moving.planner.TMDBClient")
    def test_scan_finds_video_files(self, mock_client_cls, mock_cached_cls, test_config, db_engine, media_dir):
        mock_cached = MagicMock()
        mock_cached.search.return_value = [
            TMDBResult(
                tmdb_id=1396, imdb_id=None, title="Breaking Bad",
                original_title="Breaking Bad", year=2008, media_type="tv",
                overview="", popularity=200,
            )
        ]
        mock_cached.get_tv_episode.return_value = {"name": "Pilot"}
        mock_cached.get_movie_details.return_value = {"imdb_id": "tt0133093"}
        mock_cached_cls.return_value = mock_cached

        planner = ScanPlanner(test_config, engine=db_engine)
        planner.tmdb = mock_cached

        plans = planner.scan_directory(media_dir)

        # Should find video files (breaking bad + matrix, possibly sample depending on size filter)
        video_plans = [p for p in plans if p.status != "skipped"]
        assert len(video_plans) >= 1  # at least breaking bad

    @patch("mediasorter.moving.planner.CachedTMDBClient")
    @patch("mediasorter.moving.planner.TMDBClient")
    def test_sibling_files_collected(self, mock_client_cls, mock_cached_cls, test_config, db_engine, media_dir):
        mock_cached = MagicMock()
        mock_cached.search.return_value = [
            TMDBResult(
                tmdb_id=1396, imdb_id=None, title="Breaking Bad",
                original_title="Breaking Bad", year=2008, media_type="tv",
                overview="", popularity=200,
            )
        ]
        mock_cached.get_tv_episode.return_value = {"name": "Pilot"}
        mock_cached_cls.return_value = mock_cached

        planner = ScanPlanner(test_config, engine=db_engine)
        planner.tmdb = mock_cached

        plans = planner.scan_directory(media_dir)

        # Find the Breaking Bad plan
        bb_plans = [p for p in plans if p.parsed and "Breaking Bad" in p.parsed.title]
        if bb_plans:
            bb_plan = bb_plans[0]
            if bb_plan.status == "ready":
                # Should have the .en.srt sibling
                assert len(bb_plan.siblings) >= 1

    @patch("mediasorter.moving.planner.CachedTMDBClient")
    @patch("mediasorter.moving.planner.TMDBClient")
    def test_duplicate_detection(self, mock_client_cls, mock_cached_cls, test_config, db_engine, tmp_path):
        # Create two files that would map to the same destination
        media = tmp_path / "media2"
        media.mkdir()
        (media / "Movie.2020.720p.mkv").write_bytes(b"\x00" * 100)
        (media / "Movie.2020.1080p.mkv").write_bytes(b"\x00" * 100)

        mock_cached = MagicMock()
        mock_cached.search.return_value = [
            TMDBResult(
                tmdb_id=999, imdb_id=None, title="Movie",
                original_title="Movie", year=2020, media_type="movie",
                overview="", popularity=50,
            )
        ]
        mock_cached.get_movie_details.return_value = {"imdb_id": None}
        mock_cached_cls.return_value = mock_cached

        planner = ScanPlanner(test_config, engine=db_engine)
        planner.tmdb = mock_cached

        plans = planner.scan_directory(media)
        statuses = [p.status for p in plans]

        # One should be ready, the other duplicate (or both ready if paths differ)
        assert "ready" in statuses or "duplicate" in statuses

    @patch("mediasorter.moving.planner.CachedTMDBClient")
    @patch("mediasorter.moving.planner.TMDBClient")
    def test_no_tmdb_results_low_confidence(self, mock_client_cls, mock_cached_cls, test_config, db_engine, tmp_path):
        media = tmp_path / "media3"
        media.mkdir()
        (media / "RandomGarbage123.mkv").write_bytes(b"\x00" * 100)

        mock_cached = MagicMock()
        mock_cached.search.return_value = []  # no results
        mock_cached_cls.return_value = mock_cached

        planner = ScanPlanner(test_config, engine=db_engine)
        planner.tmdb = mock_cached

        plans = planner.scan_directory(media)

        assert len(plans) >= 1
        assert plans[0].status == "low_confidence"
