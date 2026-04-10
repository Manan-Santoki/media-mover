"""Move planner — builds dry-run plan of source -> dest moves.

Orchestrates the full pipeline: scan directory -> parse filenames ->
search TMDB -> score matches -> build canonical paths -> detect duplicates.
Outputs a list of MovePlan objects that can be rendered or executed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import structlog
from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

from mediasorter.config import AppConfig
from mediasorter.db.engine import get_engine, create_tables, get_session
from mediasorter.db.models import MediaFile, ParseResult, TMDBMatch
from mediasorter.matching.scorer import best_match
from mediasorter.matching.tmdb_client import CachedTMDBClient, TMDBClient, TMDBResult
from mediasorter.parsing.guessit_wrapper import ParsedMedia, parse_filename
from mediasorter.parsing.normalize import format_episode_code, sanitize_filename
from mediasorter.utils.fs import (
    VIDEO_EXTENSIONS,
    find_sibling_files,
    is_file_in_use,
    is_incomplete_file,
    is_sample_file,
    is_video_file,
)
from mediasorter.utils.rate_limit import TokenBucket

log = structlog.get_logger(__name__)


@dataclass
class MovePlan:
    """A planned file move with metadata."""

    source: Path
    dest: Path
    siblings: list[tuple[Path, Path]] = field(default_factory=list)
    media_type: str = ""
    parsed: ParsedMedia | None = None
    tmdb_match: TMDBResult | None = None
    confidence: float = 0.0
    status: Literal["ready", "low_confidence", "duplicate", "skipped", "error"] = "ready"
    reason: str | None = None


def build_movie_path(
    movies_root: Path,
    title: str,
    year: int | None,
    imdb_id: str | None,
    tmdb_id: int,
    ext: str,
) -> Path:
    """Build Jellyfin-canonical movie path.

    Format: Movies/Movie Title (Year) [imdbid-tt1234567]/Movie Title (Year).ext
    Falls back to [tmdbid-N] if no IMDB ID available.
    """
    safe_title = sanitize_filename(title)

    if year:
        folder_name = f"{safe_title} ({year})"
    else:
        folder_name = safe_title

    if imdb_id:
        folder_name += f" [imdbid-{imdb_id}]"
    else:
        folder_name += f" [tmdbid-{tmdb_id}]"

    filename = f"{safe_title} ({year}){ext}" if year else f"{safe_title}{ext}"

    return movies_root / folder_name / filename


def build_episode_path(
    tv_root: Path,
    series_title: str,
    series_year: int | None,
    tmdb_id: int,
    season: int,
    episodes: list[int],
    episode_title: str | None,
    ext: str,
) -> Path:
    """Build Jellyfin-canonical episode path.

    Format: Shows/Series Name (Year) [tmdbid-N]/Season NN/Series Name - SNNENN - Episode Title.ext
    """
    safe_series = sanitize_filename(series_title)

    if series_year:
        series_folder = f"{safe_series} ({series_year}) [tmdbid-{tmdb_id}]"
    else:
        series_folder = f"{safe_series} [tmdbid-{tmdb_id}]"

    season_folder = f"Season {season:02d}"

    ep_code = format_episode_code(season, episodes)
    if episode_title:
        safe_ep_title = sanitize_filename(episode_title)
        filename = f"{safe_series} - {ep_code} - {safe_ep_title}{ext}"
    else:
        filename = f"{safe_series} - {ep_code}{ext}"

    return tv_root / series_folder / season_folder / filename


def build_sibling_dest(
    sibling_source: Path,
    video_source: Path,
    video_dest: Path,
) -> Path:
    """Build destination path for a sibling file (subtitle, nfo, etc).

    Preserves the suffix difference relative to the video file.
    E.g. Movie.en.srt -> dest_dir/Movie Title (Year).en.srt
    """
    video_stem = video_source.stem
    sibling_name = sibling_source.name

    # Extract the part after the video stem (e.g., ".en.srt")
    if sibling_name.startswith(video_stem):
        suffix_part = sibling_name[len(video_stem):]
    else:
        suffix_part = sibling_source.suffix

    dest_stem = video_dest.stem
    return video_dest.parent / f"{dest_stem}{suffix_part}"


class ScanPlanner:
    """Orchestrates scanning and plan building."""

    def __init__(self, config: AppConfig, engine=None):
        self.config = config
        self.engine = engine or get_engine()
        create_tables(self.engine)

        limiter = TokenBucket()
        from mediasorter.config import TMDBConfig
        client = TMDBClient(config.tmdb, limiter)
        self.tmdb = CachedTMDBClient(client, self.engine, config.tmdb.cache_ttl_days)
        self._dest_registry: set[str] = set()  # track planned destinations

    def scan_directory(
        self,
        root: Path,
        media_type: str = "both",
        since: datetime | None = None,
    ) -> list[MovePlan]:
        """Walk root directory and build move plans for all video files."""
        run_id = str(uuid.uuid4())
        plans: list[MovePlan] = []

        log.info("scan_started", root=str(root), media_type=media_type, run_id=run_id)

        video_files = self._collect_video_files(root)
        log.info("files_found", count=len(video_files))

        for filepath in video_files:
            if since and filepath.stat().st_mtime < since.timestamp():
                continue

            plan = self._process_file(filepath, media_type)
            plans.append(plan)

        # Sort: ready first, then by confidence descending
        plans.sort(key=lambda p: (p.status != "ready", -p.confidence))

        log.info(
            "scan_complete",
            total=len(plans),
            ready=sum(1 for p in plans if p.status == "ready"),
            low_confidence=sum(1 for p in plans if p.status == "low_confidence"),
            skipped=sum(1 for p in plans if p.status == "skipped"),
            errors=sum(1 for p in plans if p.status == "error"),
        )

        return plans

    def _collect_video_files(self, root: Path) -> list[Path]:
        """Recursively find all video files under root."""
        files = []
        try:
            for path in sorted(root.rglob("*")):
                if path.is_file() and is_video_file(path):
                    files.append(path)
        except OSError as e:
            log.error("scan_walk_error", root=str(root), error=str(e))
        return files

    def _process_file(self, filepath: Path, filter_type: str) -> MovePlan:
        """Process a single file through the full pipeline."""
        ext = filepath.suffix

        # Skip checks
        if is_incomplete_file(filepath):
            return MovePlan(
                source=filepath, dest=filepath,
                status="skipped", reason="File appears incomplete or still downloading",
            )

        if is_file_in_use(filepath):
            return MovePlan(
                source=filepath, dest=filepath,
                status="skipped", reason="File is currently in use",
            )

        # Parse
        try:
            parsed = parse_filename(filepath)
        except Exception as e:
            return MovePlan(
                source=filepath, dest=filepath,
                status="error", reason=f"Parse error: {e}",
            )

        # Filter by type
        if filter_type != "both" and parsed.media_type != filter_type:
            return MovePlan(
                source=filepath, dest=filepath, parsed=parsed,
                status="skipped", reason=f"Filtered out (type={parsed.media_type})",
            )

        # Check if sample
        if is_sample_file(
            filepath,
            parsed.media_type,
            self.config.matching.min_movie_size_mb,
            self.config.matching.min_episode_size_mb,
        ):
            return MovePlan(
                source=filepath, dest=filepath, parsed=parsed,
                status="skipped", reason="Sample file (too small or name contains 'sample')",
            )

        # Search TMDB
        results = self.tmdb.search(parsed)
        if not results:
            return MovePlan(
                source=filepath, dest=filepath, parsed=parsed,
                media_type=parsed.media_type,
                status="low_confidence", reason="No TMDB results found",
            )

        # Score and pick best match
        match, confidence = best_match(
            parsed, results, self.config.matching.confidence_threshold
        )

        if match is None:
            # Below threshold — still report the best candidate
            from mediasorter.matching.scorer import score_match as _score
            best_result = max(results, key=lambda r: _score(parsed, r))
            best_conf = _score(parsed, best_result)
            return MovePlan(
                source=filepath, dest=filepath, parsed=parsed,
                tmdb_match=best_result, confidence=best_conf,
                media_type=parsed.media_type,
                status="low_confidence",
                reason=f"Best match confidence {best_conf:.2f} below threshold {self.config.matching.confidence_threshold}",
            )

        # Get episode title if applicable
        episode_title = parsed.episode_title
        if parsed.media_type == "episode" and parsed.episodes and not episode_title:
            ep_details = self.tmdb.get_tv_episode(
                match.tmdb_id, parsed.season or 1, parsed.episodes[0]
            )
            if ep_details:
                episode_title = ep_details.get("name")

        # Build destination path
        if parsed.media_type == "movie":
            # Fetch IMDB ID for movies
            movie_details = self.tmdb.get_movie_details(match.tmdb_id)
            imdb_id = movie_details.get("imdb_id") if movie_details else None

            dest = build_movie_path(
                movies_root=self.config.roots.movies,
                title=match.title,
                year=match.year,
                imdb_id=imdb_id,
                tmdb_id=match.tmdb_id,
                ext=ext,
            )
        else:
            dest = build_episode_path(
                tv_root=self.config.roots.shows,
                series_title=match.title,
                series_year=match.year,
                tmdb_id=match.tmdb_id,
                season=parsed.season or 1,
                episodes=parsed.episodes,
                episode_title=episode_title,
                ext=ext,
            )

        # Check if already at correct location
        if filepath.resolve() == dest.resolve():
            return MovePlan(
                source=filepath, dest=dest, parsed=parsed,
                tmdb_match=match, confidence=confidence,
                media_type=parsed.media_type,
                status="skipped", reason="Already at correct location",
            )

        # Check for duplicate destination
        dest_str = str(dest)
        if dest_str in self._dest_registry:
            return MovePlan(
                source=filepath, dest=dest, parsed=parsed,
                tmdb_match=match, confidence=confidence,
                media_type=parsed.media_type,
                status="duplicate",
                reason=f"Destination already planned for another file",
            )
        self._dest_registry.add(dest_str)

        # Collect sibling files
        siblings = []
        for sib in find_sibling_files(filepath):
            sib_dest = build_sibling_dest(sib, filepath, dest)
            siblings.append((sib, sib_dest))

        return MovePlan(
            source=filepath, dest=dest,
            siblings=siblings,
            media_type=parsed.media_type,
            parsed=parsed,
            tmdb_match=match,
            confidence=confidence,
            status="ready",
        )

    def persist_plan(self, plans: list[MovePlan], run_id: str) -> None:
        """Store plan results in the database."""
        with get_session(self.engine) as session:
            for plan in plans:
                if plan.parsed is None:
                    continue

                mf = MediaFile(
                    source_path=str(plan.source),
                    file_size=plan.source.stat().st_size if plan.source.exists() else 0,
                    media_type=plan.media_type,
                    run_id=run_id,
                )
                session.add(mf)
                session.flush()

                pr = ParseResult(
                    media_file_id=mf.id,
                    guessit_title=plan.parsed.title,
                    guessit_year=plan.parsed.year,
                    guessit_season=plan.parsed.season,
                    guessit_episode=json.dumps(plan.parsed.episodes),
                    guessit_episode_title=plan.parsed.episode_title,
                    guessit_type=plan.parsed.media_type,
                    absolute_episode=plan.parsed.absolute_episode,
                    raw_json=json.dumps(plan.parsed.raw, default=str),
                )
                session.add(pr)

                if plan.tmdb_match:
                    tm = TMDBMatch(
                        media_file_id=mf.id,
                        tmdb_id=plan.tmdb_match.tmdb_id,
                        tmdb_type=plan.tmdb_match.media_type,
                        imdb_id=plan.tmdb_match.imdb_id,
                        matched_title=plan.tmdb_match.title,
                        matched_year=plan.tmdb_match.year,
                        confidence=plan.confidence,
                        match_source="tmdb",
                        dest_path=str(plan.dest),
                    )
                    session.add(tm)


def render_plan_table(plans: list[MovePlan], console: Console | None = None) -> None:
    """Print a rich table showing the move plan."""
    console = console or Console()

    table = Table(title="Move Plan", show_lines=True)
    table.add_column("Status", style="bold", width=14)
    table.add_column("Source", style="dim", max_width=50, overflow="fold")
    table.add_column("Destination", max_width=50, overflow="fold")
    table.add_column("Match", max_width=30)
    table.add_column("Conf", justify="right", width=6)

    status_styles = {
        "ready": "green",
        "low_confidence": "yellow",
        "duplicate": "red",
        "skipped": "dim",
        "error": "red bold",
    }

    for plan in plans:
        style = status_styles.get(plan.status, "")
        match_info = ""
        if plan.tmdb_match:
            match_info = f"{plan.tmdb_match.title}"
            if plan.tmdb_match.year:
                match_info += f" ({plan.tmdb_match.year})"

        conf_str = f"{plan.confidence:.2f}" if plan.confidence > 0 else ""
        reason = f"\n[dim]{plan.reason}[/dim]" if plan.reason and plan.status != "ready" else ""

        table.add_row(
            f"[{style}]{plan.status}[/{style}]",
            str(plan.source.name),
            str(plan.dest.name) if plan.dest != plan.source else "[dim]—[/dim]",
            match_info,
            conf_str,
        )

    console.print(table)

    # Summary
    total = len(plans)
    ready = sum(1 for p in plans if p.status == "ready")
    low = sum(1 for p in plans if p.status == "low_confidence")
    dup = sum(1 for p in plans if p.status == "duplicate")
    skipped = sum(1 for p in plans if p.status == "skipped")
    errors = sum(1 for p in plans if p.status == "error")

    console.print(
        f"\n[bold]Summary:[/bold] {total} files scanned | "
        f"[green]{ready} ready[/green] | "
        f"[yellow]{low} low confidence[/yellow] | "
        f"[red]{dup} duplicate[/red] | "
        f"[dim]{skipped} skipped[/dim] | "
        f"[red]{errors} errors[/red]"
    )


def render_plan_json(plans: list[MovePlan]) -> str:
    """Render plan as JSON for machine consumption."""
    data = []
    for plan in plans:
        item = {
            "source": str(plan.source),
            "dest": str(plan.dest),
            "status": plan.status,
            "confidence": plan.confidence,
            "media_type": plan.media_type,
            "reason": plan.reason,
        }
        if plan.tmdb_match:
            item["tmdb"] = {
                "id": plan.tmdb_match.tmdb_id,
                "title": plan.tmdb_match.title,
                "year": plan.tmdb_match.year,
                "type": plan.tmdb_match.media_type,
            }
        if plan.siblings:
            item["siblings"] = [
                {"source": str(s), "dest": str(d)} for s, d in plan.siblings
            ]
        data.append(item)

    return json.dumps(data, indent=2)
