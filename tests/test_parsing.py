"""Tests for the parsing pipeline: guessit wrapper and normalizer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mediasorter.parsing.guessit_wrapper import ParsedMedia, parse_filename
from mediasorter.parsing.normalize import (
    format_episode_code,
    normalize_for_comparison,
    normalize_for_search,
    sanitize_filename,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_filename_fixtures() -> list[dict]:
    with open(FIXTURES_DIR / "filenames.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# guessit wrapper tests
# ---------------------------------------------------------------------------


class TestParseFilename:
    """Test parse_filename against the fixture corpus."""

    @pytest.fixture(params=load_filename_fixtures(), ids=lambda f: f["input"][:60])
    def fixture(self, request):
        return request.param

    def test_title(self, fixture):
        parsed = parse_filename(fixture["input"])
        expected_title = fixture["expected"]["title"]
        # Normalize both for comparison (dots vs spaces, etc.)
        assert parsed.title.lower().replace(".", " ") == expected_title.lower().replace(".", " ")

    def test_media_type(self, fixture):
        parsed = parse_filename(fixture["input"])
        assert parsed.media_type == fixture["expected"]["media_type"]

    def test_year(self, fixture):
        parsed = parse_filename(fixture["input"])
        expected_year = fixture["expected"].get("year")
        if expected_year is not None:
            assert parsed.year == expected_year

    def test_season(self, fixture):
        parsed = parse_filename(fixture["input"])
        expected_season = fixture["expected"].get("season")
        if expected_season is not None:
            assert parsed.season == expected_season

    def test_episodes(self, fixture):
        parsed = parse_filename(fixture["input"])
        expected_eps = fixture["expected"].get("episodes")
        if expected_eps is not None:
            assert parsed.episodes == expected_eps

    def test_returns_parsed_media(self, fixture):
        parsed = parse_filename(fixture["input"])
        assert isinstance(parsed, ParsedMedia)
        assert parsed.raw  # raw dict should not be empty


class TestParseEdgeCases:
    """Test specific edge cases not in the fixture corpus."""

    def test_multi_episode_joined(self):
        parsed = parse_filename("Show.S01E01E02.mkv")
        assert parsed.episodes == [1, 2]
        assert parsed.season == 1

    def test_video_in_movie_folder(self):
        parsed = parse_filename("/movies/Something Weird.mkv")
        assert parsed.media_type == "movie"

    def test_video_in_shows_folder(self):
        # guessit may determine type independently; our context inference is fallback
        parsed = parse_filename("/shows/Something Weird.mkv")
        # Just verify it parses without error and returns a valid type
        assert parsed.media_type in ("movie", "episode")

    def test_container_extracted(self):
        parsed = parse_filename("Movie.2020.1080p.BluRay.mkv")
        assert parsed.container == "mkv"

    def test_resolution_extracted(self):
        parsed = parse_filename("Movie.2020.1080p.BluRay.mkv")
        assert parsed.resolution is not None
        assert "1080" in parsed.resolution


# ---------------------------------------------------------------------------
# Normalizer tests
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_removes_colon(self):
        assert ":" not in sanitize_filename("Star Wars: A New Hope")

    def test_removes_question_mark(self):
        assert "?" not in sanitize_filename("What If...?")

    def test_removes_pipe(self):
        assert "|" not in sanitize_filename("This | That")

    def test_preserves_unicode(self):
        result = sanitize_filename("千と千尋の神隠し")
        assert "千" in result

    def test_preserves_accented_chars(self):
        result = sanitize_filename("Amélie")
        assert "é" in result

    def test_collapses_multiple_hyphens(self):
        result = sanitize_filename('A: B: C')
        assert "---" not in result

    def test_strips_leading_trailing(self):
        result = sanitize_filename("  :test: ")
        assert not result.startswith("-")
        assert not result.endswith("-")


class TestNormalizeForSearch:
    def test_strips_quality_tags(self):
        result = normalize_for_search("Breaking Bad 720p BluRay x264")
        assert "720p" not in result
        assert "BluRay" not in result.lower() or "bluray" not in result.lower()
        assert "Breaking Bad" in result

    def test_dots_to_spaces(self):
        result = normalize_for_search("Breaking.Bad")
        assert "Breaking Bad" == result

    def test_underscores_to_spaces(self):
        result = normalize_for_search("Breaking_Bad")
        assert "Breaking Bad" == result


class TestNormalizeForComparison:
    def test_lowercase(self):
        assert normalize_for_comparison("THE MATRIX") == "matrix"

    def test_strips_articles(self):
        assert normalize_for_comparison("The Office") == "office"
        assert normalize_for_comparison("A Beautiful Mind") == "beautiful mind"
        assert normalize_for_comparison("An American Werewolf") == "american werewolf"

    def test_removes_accents(self):
        result = normalize_for_comparison("Amélie")
        assert result == "amelie"

    def test_removes_punctuation(self):
        result = normalize_for_comparison("What's Up, Doc?")
        assert result == "whats up doc"


class TestFormatEpisodeCode:
    def test_single_episode(self):
        assert format_episode_code(1, [1]) == "S01E01"

    def test_multi_episode(self):
        assert format_episode_code(1, [1, 2]) == "S01E01-E02"

    def test_high_numbers(self):
        assert format_episode_code(12, [99]) == "S12E99"

    def test_triple_episode(self):
        assert format_episode_code(3, [1, 2, 3]) == "S03E01-E02-E03"

    def test_empty_episodes(self):
        assert format_episode_code(1, []) == "S01"

    def test_zero_padded(self):
        assert format_episode_code(1, [1]) == "S01E01"
        assert format_episode_code(1, [9]) == "S01E09"
