"""Health check HTTP endpoint for daemon mode.

Provides a /health endpoint for uptime monitoring. Only active
when running in daemon mode. Binds to localhost by default.
"""

from __future__ import annotations

import threading
from datetime import datetime

import structlog
import uvicorn
from fastapi import FastAPI

log = structlog.get_logger(__name__)

health_app = FastAPI(title="MediaSorter Health", docs_url=None, redoc_url=None)

_scheduler_ref = None
_start_time = datetime.now()


@health_app.get("/health")
def health():
    """Health check endpoint."""
    jobs = []
    if _scheduler_ref:
        for job in _scheduler_ref.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            })

    return {
        "status": "ok",
        "uptime_seconds": (datetime.now() - _start_time).total_seconds(),
        "started_at": _start_time.isoformat(),
        "jobs": jobs,
    }


def start_health_server(port: int, scheduler=None) -> None:
    """Start the health server in a background thread."""
    global _scheduler_ref
    _scheduler_ref = scheduler

    def _run():
        uvicorn.run(
            health_app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )

    thread = threading.Thread(target=_run, daemon=True, name="health-server")
    thread.start()
    log.info("health_server_started", port=port)
