"""Upcoming episode tracker using TMDB.

Queries TMDB for next_episode_to_air for all tracked shows, stores
upcoming episodes in SQLite, and sends webhook notifications for
episodes airing within the configured notification window.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import structlog
from sqlmodel import Session, select

from mediasorter.config import AppConfig
from mediasorter.db.models import TMDBMatch, UpcomingEpisode
from mediasorter.matching.tmdb_client import CachedTMDBClient
from mediasorter.notifications.webhook import send_webhook

log = structlog.get_logger(__name__)


class UpcomingTracker:
    """Tracks and notifies about upcoming episodes for shows in the library."""

    def __init__(self, config: AppConfig, tmdb: CachedTMDBClient, engine):
        self._config = config
        self._tmdb = tmdb
        self._engine = engine

    def check_upcoming(self, notify: bool = False) -> list[dict]:
        """Check for upcoming episodes across all tracked shows.

        Args:
            notify: If True, send webhook notifications for upcoming episodes.

        Returns:
            List of upcoming episode dicts.
        """
        upcoming = []

        # Get all unique TV show TMDB IDs from our database
        show_ids = self._get_tracked_shows()
        log.info("checking_upcoming", show_count=len(show_ids))

        for tmdb_id, show_title in show_ids:
            ep_info = self._check_show(tmdb_id, show_title)
            if ep_info:
                upcoming.append(ep_info)

                if notify and not ep_info.get("already_notified"):
                    self._notify(ep_info)
                    self._mark_notified(ep_info)

        log.info("upcoming_check_complete", upcoming_count=len(upcoming))
        return upcoming

    def _get_tracked_shows(self) -> list[tuple[int, str]]:
        """Get all unique TV show TMDB IDs from the database."""
        with Session(self._engine) as session:
            stmt = (
                select(TMDBMatch.tmdb_id, TMDBMatch.matched_title)
                .where(TMDBMatch.tmdb_type == "tv")
                .distinct()
            )
            results = session.exec(stmt).all()
            return [(r[0], r[1]) for r in results]

    def _check_show(self, tmdb_id: int, show_title: str) -> dict | None:
        """Check a single show for upcoming episodes."""
        details = self._tmdb.get_tv_details(tmdb_id)
        if not details:
            return None

        next_ep = details.get("next_episode_to_air")
        if not next_ep:
            return None

        air_date_str = next_ep.get("air_date", "")
        if not air_date_str:
            return None

        try:
            air_date = date.fromisoformat(air_date_str)
        except ValueError:
            return None

        # Check if within notification window
        window = timedelta(days=self._config.notifications.window_days)
        if air_date - date.today() > window:
            return None

        # Check if already notified
        already_notified = self._is_notified(tmdb_id, next_ep.get("season_number", 0), next_ep.get("episode_number", 0))

        # Build the library path for the show
        library_path = str(self._config.roots.shows / f"{show_title} [tmdbid-{tmdb_id}]")

        return {
            "tmdb_id": tmdb_id,
            "show_title": show_title,
            "season": next_ep.get("season_number", 0),
            "episode": next_ep.get("episode_number", 0),
            "episode_name": next_ep.get("name", ""),
            "air_date": air_date_str,
            "library_path": library_path,
            "already_notified": already_notified,
        }

    def _is_notified(self, tmdb_id: int, season: int, episode: int) -> bool:
        """Check if we've already sent a notification for this episode."""
        with Session(self._engine) as session:
            stmt = select(UpcomingEpisode).where(
                UpcomingEpisode.tmdb_id == tmdb_id,
                UpcomingEpisode.season == season,
                UpcomingEpisode.episode == episode,
                UpcomingEpisode.notified == True,
            )
            return session.exec(stmt).first() is not None

    def _mark_notified(self, ep_info: dict) -> None:
        """Mark an episode as notified in the database."""
        with Session(self._engine) as session:
            # Upsert
            stmt = select(UpcomingEpisode).where(
                UpcomingEpisode.tmdb_id == ep_info["tmdb_id"],
                UpcomingEpisode.season == ep_info["season"],
                UpcomingEpisode.episode == ep_info["episode"],
            )
            existing = session.exec(stmt).first()

            if existing:
                existing.notified = True
                existing.notified_at = datetime.now()
                session.add(existing)
            else:
                session.add(UpcomingEpisode(
                    tmdb_id=ep_info["tmdb_id"],
                    show_title=ep_info["show_title"],
                    season=ep_info["season"],
                    episode=ep_info["episode"],
                    episode_name=ep_info.get("episode_name"),
                    air_date=ep_info["air_date"],
                    notified=True,
                    notified_at=datetime.now(),
                ))
            session.commit()

    def _notify(self, ep_info: dict) -> None:
        """Send webhook notification for an upcoming episode."""
        webhook_config = self._config.webhooks.pickrr

        payload = {
            "show": {
                "tmdb_id": ep_info["tmdb_id"],
                "title": ep_info["show_title"],
            },
            "episode": {
                "season": ep_info["season"],
                "number": ep_info["episode"],
                "name": ep_info.get("episode_name", ""),
                "air_date": ep_info["air_date"],
            },
            "library_path": ep_info.get("library_path", ""),
        }

        send_webhook(webhook_config, "upcoming_episode", payload)
