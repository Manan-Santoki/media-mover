"""SQLModel table definitions for MediaSorter state database.

Tables:
- MediaFile: tracks known media files
- ParseResult: guessit parse output per file
- TMDBMatch: best TMDB match per file
- MoveLog: audit trail of file moves (supports rollback)
- TMDBCache: cached TMDB API responses with TTL
- UpcomingEpisode: tracked upcoming episodes for notifications
- RunLog: per-run summary statistics
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class MediaFile(SQLModel, table=True):
    __tablename__ = "media_file"

    id: int | None = Field(default=None, primary_key=True)
    source_path: str = Field(index=True, unique=True)
    file_size: int = 0
    file_hash: str | None = None
    media_type: str = ""  # "movie" | "episode"
    run_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ParseResult(SQLModel, table=True):
    __tablename__ = "parse_result"

    id: int | None = Field(default=None, primary_key=True)
    media_file_id: int = Field(foreign_key="media_file.id", index=True)
    guessit_title: str = ""
    guessit_year: int | None = None
    guessit_season: int | None = None
    guessit_episode: str | None = None  # JSON list, e.g. "[1]" or "[1,2]"
    guessit_episode_title: str | None = None
    guessit_type: str = ""  # "movie" | "episode"
    absolute_episode: int | None = None
    raw_json: str = "{}"


class TMDBMatch(SQLModel, table=True):
    __tablename__ = "tmdb_match"

    id: int | None = Field(default=None, primary_key=True)
    media_file_id: int = Field(foreign_key="media_file.id", index=True)
    tmdb_id: int = 0
    tmdb_type: str = ""  # "movie" | "tv"
    imdb_id: str | None = None
    matched_title: str = ""
    matched_year: int | None = None
    confidence: float = 0.0
    match_source: str = "tmdb"  # "tmdb" | "openrouter" | "manual"
    dest_path: str | None = None


class MoveLog(SQLModel, table=True):
    __tablename__ = "move_log"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    media_file_id: int = Field(foreign_key="media_file.id", index=True)
    source_path: str = ""
    dest_path: str = ""
    moved_at: datetime = Field(default_factory=datetime.utcnow)
    rolled_back: bool = False
    error: str | None = None


class TMDBCache(SQLModel, table=True):
    __tablename__ = "tmdb_cache"

    id: int | None = Field(default=None, primary_key=True)
    cache_key: str = Field(index=True, unique=True)
    response_json: str = "{}"
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class UpcomingEpisode(SQLModel, table=True):
    __tablename__ = "upcoming_episode"

    id: int | None = Field(default=None, primary_key=True)
    tmdb_id: int = Field(index=True)
    show_title: str = ""
    season: int = 0
    episode: int = 0
    episode_name: str | None = None
    air_date: str | None = None
    notified: bool = False
    notified_at: datetime | None = None


class RunLog(SQLModel, table=True):
    __tablename__ = "run_log"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True, unique=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    files_scanned: int = 0
    files_matched: int = 0
    files_moved: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    ai_calls: int = 0
    estimated_cost: float = 0.0
