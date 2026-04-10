"""APScheduler-based daemon mode.

Runs organize and check-upcoming on configurable cron schedules.
Gracefully handles mount failures (log + skip, don't crash).
"""

from __future__ import annotations

import signal
import uuid

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from mediasorter.config import AppConfig
from mediasorter.db.engine import create_tables, get_engine
from mediasorter.logging import bind_run_id, configure_logging
from mediasorter.matching.tmdb_client import CachedTMDBClient, TMDBClient
from mediasorter.moving.executor import MoveExecutor
from mediasorter.moving.planner import ScanPlanner
from mediasorter.notifications.upcoming import UpcomingTracker
from mediasorter.notifications.webhook import send_webhook
from mediasorter.utils.fs import check_mount
from mediasorter.utils.rate_limit import TokenBucket

log = structlog.get_logger(__name__)


def run_organize(config: AppConfig, engine) -> None:
    """Run a single organize cycle."""
    run_id = str(uuid.uuid4())
    bind_run_id(run_id)
    log.info("daemon_organize_start", run_id=run_id)

    # Check mount health
    for root_name, root_path in [("shows", config.roots.shows), ("movies", config.roots.movies)]:
        if root_path.exists() and not check_mount(root_path):
            log.error("mount_unhealthy", root=root_name, path=str(root_path))
            send_webhook(config.webhooks.pickrr, "error", {
                "error": f"Mount unhealthy: {root_path}",
                "run_id": run_id,
            })
            return

    try:
        planner = ScanPlanner(config, engine=engine)

        # Scan both roots
        all_plans = []
        for root in [config.roots.shows, config.roots.movies]:
            if root.exists():
                plans = planner.scan_directory(root)
                all_plans.extend(plans)

        # Persist plans
        planner.persist_plan(all_plans, run_id)

        # Execute if apply is enabled
        ready = [p for p in all_plans if p.status == "ready"]
        if config.moving.apply and ready:
            executor = MoveExecutor(engine=engine, config=config.moving, jellyfin_config=config.jellyfin)
            results = executor.execute_plan(ready, run_id=run_id)
            moved = sum(1 for r in results if r.success)
            failed = sum(1 for r in results if not r.success)
            log.info("daemon_organize_done", moved=moved, failed=failed, run_id=run_id)

            send_webhook(config.webhooks.pickrr, "files_moved", {
                "run_id": run_id,
                "moved": moved,
                "failed": failed,
            })
        else:
            log.info("daemon_organize_dry_run", ready=len(ready), total=len(all_plans))

        send_webhook(config.webhooks.pickrr, "scan_complete", {
            "run_id": run_id,
            "total": len(all_plans),
            "ready": len(ready),
        })

    except Exception as e:
        log.error("daemon_organize_error", error=str(e), run_id=run_id)
        send_webhook(config.webhooks.pickrr, "error", {
            "error": str(e),
            "run_id": run_id,
        })


def run_upcoming(config: AppConfig, engine) -> None:
    """Run a single upcoming episode check."""
    run_id = str(uuid.uuid4())
    bind_run_id(run_id)
    log.info("daemon_upcoming_start", run_id=run_id)

    try:
        client = TMDBClient(config.tmdb, TokenBucket())
        cached = CachedTMDBClient(client, engine, config.tmdb.cache_ttl_days)
        tracker = UpcomingTracker(config, cached, engine)
        upcoming = tracker.check_upcoming(notify=True)
        log.info("daemon_upcoming_done", count=len(upcoming), run_id=run_id)
    except Exception as e:
        log.error("daemon_upcoming_error", error=str(e), run_id=run_id)


def run_daemon(config: AppConfig) -> None:
    """Start the daemon with scheduled jobs."""
    configure_logging(
        level=config.logging.level,
        json_output=True,
        log_file=config.logging.file,
    )

    engine = get_engine()
    create_tables(engine)

    scheduler = BackgroundScheduler()

    # Parse cron expressions
    organize_cron = CronTrigger.from_crontab(config.daemon.organize_cron)
    upcoming_cron = CronTrigger.from_crontab(config.daemon.upcoming_cron)

    scheduler.add_job(
        run_organize,
        trigger=organize_cron,
        args=[config, engine],
        id="organize",
        name="Organize media library",
    )

    scheduler.add_job(
        run_upcoming,
        trigger=upcoming_cron,
        args=[config, engine],
        id="upcoming",
        name="Check upcoming episodes",
    )

    scheduler.start()
    log.info(
        "daemon_started",
        organize_cron=config.daemon.organize_cron,
        upcoming_cron=config.daemon.upcoming_cron,
        health_port=config.daemon.health_port,
    )

    # Start health endpoint in a thread
    from mediasorter.daemon.health import start_health_server

    start_health_server(config.daemon.health_port, scheduler)

    # Wait for shutdown signal
    shutdown = False

    def _handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        log.info("shutdown_signal_received", signal=signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        import time
        while not shutdown:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        log.info("daemon_stopped")
