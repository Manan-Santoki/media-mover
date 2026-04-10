"""File move executor with rollback support.

Executes MovePlans by actually moving files on the filesystem.
Every move is logged in the MoveLog table for reversibility.
Never deletes files — duplicates and orphans go to trash.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from sqlmodel import Session, select

from mediasorter.config import JellyfinConfig, MovingConfig
from mediasorter.db.models import MoveLog
from mediasorter.moving.planner import MovePlan
from mediasorter.utils.fs import safe_remove_empty_dirs

log = structlog.get_logger(__name__)


@dataclass
class MoveResult:
    """Result of a single file move operation."""

    source: Path
    dest: Path
    success: bool
    error: str | None = None


class MoveExecutor:
    """Executes move plans and supports rollback."""

    def __init__(
        self,
        engine,
        config: MovingConfig,
        jellyfin_config: JellyfinConfig | None = None,
    ):
        self._engine = engine
        self._config = config
        self._jellyfin = jellyfin_config

    def execute_plan(self, plans: list[MovePlan], run_id: str) -> list[MoveResult]:
        """Execute all 'ready' plans. Returns list of results."""
        results = []

        for plan in plans:
            if plan.status != "ready":
                continue
            result = self.execute_one(plan, run_id)
            results.append(result)

            # Also move sibling files
            for sib_src, sib_dest in plan.siblings:
                sib_result = self._move_file(sib_src, sib_dest, run_id, plan)
                if not sib_result.success:
                    log.warning(
                        "sibling_move_failed",
                        source=str(sib_src),
                        error=sib_result.error,
                    )

        # Trigger Jellyfin library refresh if configured
        moved_count = sum(1 for r in results if r.success)
        if moved_count > 0 and self._jellyfin and self._jellyfin.url:
            self._refresh_jellyfin()

        return results

    def execute_one(self, plan: MovePlan, run_id: str) -> MoveResult:
        """Move a single file and record in MoveLog."""
        return self._move_file(plan.source, plan.dest, run_id, plan)

    def _move_file(
        self,
        source: Path,
        dest: Path,
        run_id: str,
        plan: MovePlan | None = None,
    ) -> MoveResult:
        """Perform the actual file move with safety checks."""
        try:
            # Verify source exists
            if not source.exists():
                return MoveResult(source, dest, False, "Source file not found")

            # Create destination directory
            dest.parent.mkdir(parents=True, exist_ok=True)

            # Handle existing destination
            if dest.exists():
                if self._files_identical(source, dest):
                    log.info("skip_identical", source=str(source), dest=str(dest))
                    return MoveResult(source, dest, True)

                # Rename with .dup suffix
                dest = self._deduplicate_path(dest)
                log.warning("dest_exists_dedup", source=str(source), dest=str(dest))

            # Move the file
            log.info("moving_file", source=str(source), dest=str(dest))
            shutil.move(str(source), str(dest))

            # Record in MoveLog
            self._record_move(run_id, source, dest, plan)

            # Clean up empty source directories
            safe_remove_empty_dirs(source.parent, source.parent.parent.parent)

            return MoveResult(source, dest, True)

        except OSError as e:
            error_msg = f"Move failed: {e}"
            log.error("move_failed", source=str(source), dest=str(dest), error=str(e))
            self._record_move(run_id, source, dest, plan, error=error_msg)
            return MoveResult(source, dest, False, error_msg)

    def rollback_run(self, run_id: str) -> int:
        """Reverse all moves from a specific run. Returns count of reversed moves."""
        count = 0
        with Session(self._engine) as session:
            stmt = (
                select(MoveLog)
                .where(MoveLog.run_id == run_id, MoveLog.rolled_back == False, MoveLog.error == None)
                .order_by(MoveLog.moved_at.desc())
            )
            moves = session.exec(stmt).all()

            for move in moves:
                src = Path(move.source_path)
                dest = Path(move.dest_path)

                if not dest.exists():
                    log.warning("rollback_skip_missing", dest=str(dest))
                    continue

                try:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(src))
                    move.rolled_back = True
                    session.add(move)
                    count += 1
                    log.info("rolled_back", source=str(src), dest=str(dest))

                    safe_remove_empty_dirs(dest.parent, dest.parent.parent.parent)
                except OSError as e:
                    log.error("rollback_failed", dest=str(dest), error=str(e))

            session.commit()

        return count

    def _record_move(
        self,
        run_id: str,
        source: Path,
        dest: Path,
        plan: MovePlan | None,
        error: str | None = None,
    ) -> None:
        """Record a move in the database."""
        with Session(self._engine) as session:
            move_log = MoveLog(
                run_id=run_id,
                media_file_id=None,
                source_path=str(source),
                dest_path=str(dest),
                error=error,
            )
            session.add(move_log)
            session.commit()

    def _files_identical(self, a: Path, b: Path) -> bool:
        """Compare two files by size and partial hash."""
        try:
            if a.stat().st_size != b.stat().st_size:
                return False
            # Compare first 64KB hash
            return self._partial_hash(a) == self._partial_hash(b)
        except OSError:
            return False

    @staticmethod
    def _partial_hash(path: Path, chunk_size: int = 65536) -> str:
        """Hash the first chunk of a file for quick comparison."""
        h = hashlib.md5()
        with open(path, "rb") as f:
            h.update(f.read(chunk_size))
        return h.hexdigest()

    @staticmethod
    def _deduplicate_path(path: Path) -> Path:
        """Add .dup1, .dup2 etc. to avoid overwriting."""
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        for i in range(1, 100):
            candidate = parent / f"{stem}.dup{i}{suffix}"
            if not candidate.exists():
                return candidate
        return parent / f"{stem}.dup99{suffix}"

    def _refresh_jellyfin(self) -> None:
        """Trigger Jellyfin library refresh via API."""
        if not self._jellyfin or not self._jellyfin.url:
            return

        try:
            import httpx

            url = f"{self._jellyfin.url.rstrip('/')}/Library/Refresh"
            headers = {"X-Emby-Token": self._jellyfin.api_key}
            response = httpx.post(url, headers=headers, timeout=30)
            if response.status_code < 300:
                log.info("jellyfin_refresh_triggered")
            else:
                log.warning("jellyfin_refresh_failed", status=response.status_code)
        except Exception as e:
            log.warning("jellyfin_refresh_error", error=str(e))
