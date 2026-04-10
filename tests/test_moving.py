"""Tests for the file move executor and rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from mediasorter.config import JellyfinConfig, MovingConfig
from mediasorter.db.engine import create_tables, get_engine
from mediasorter.moving.executor import MoveExecutor, MoveResult
from mediasorter.moving.planner import MovePlan


@pytest.fixture
def db_engine(tmp_path):
    engine = get_engine(tmp_path / "test.db")
    create_tables(engine)
    return engine


@pytest.fixture
def moving_config(tmp_path):
    return MovingConfig(trash_dir=tmp_path / ".trash")


@pytest.fixture
def executor(db_engine, moving_config):
    return MoveExecutor(engine=db_engine, config=moving_config)


def make_plan(source: Path, dest: Path, siblings=None):
    return MovePlan(
        source=source,
        dest=dest,
        siblings=siblings or [],
        media_type="movie",
        status="ready",
    )


class TestMoveExecutor:
    def test_basic_move(self, executor, tmp_path):
        src = tmp_path / "source" / "movie.mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"\x00" * 1024)

        dest = tmp_path / "dest" / "Movie (2020)" / "Movie (2020).mkv"

        plan = make_plan(src, dest)
        results = executor.execute_plan([plan], run_id="test-run-1")

        assert len(results) == 1
        assert results[0].success
        assert dest.exists()
        assert not src.exists()

    def test_creates_dest_directory(self, executor, tmp_path):
        src = tmp_path / "movie.mkv"
        src.write_bytes(b"\x00" * 512)

        dest = tmp_path / "deep" / "nested" / "dir" / "movie.mkv"

        plan = make_plan(src, dest)
        results = executor.execute_plan([plan], run_id="test-run-2")

        assert results[0].success
        assert dest.exists()

    def test_skip_missing_source(self, executor, tmp_path):
        src = tmp_path / "nonexistent.mkv"
        dest = tmp_path / "dest.mkv"

        plan = make_plan(src, dest)
        results = executor.execute_plan([plan], run_id="test-run-3")

        assert len(results) == 1
        assert not results[0].success
        assert "not found" in results[0].error

    def test_duplicate_dest_identical(self, executor, tmp_path):
        src = tmp_path / "movie.mkv"
        src.write_bytes(b"\x00" * 1024)

        dest = tmp_path / "dest" / "movie.mkv"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"\x00" * 1024)  # identical content

        plan = make_plan(src, dest)
        results = executor.execute_plan([plan], run_id="test-run-4")

        assert results[0].success  # should succeed (skip identical)

    def test_duplicate_dest_different_gets_suffix(self, executor, tmp_path):
        src = tmp_path / "movie.mkv"
        src.write_bytes(b"\x00" * 1024)

        dest = tmp_path / "dest" / "movie.mkv"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"\xFF" * 1024)  # different content

        plan = make_plan(src, dest)
        results = executor.execute_plan([plan], run_id="test-run-5")

        assert results[0].success
        # The original dest should still have the old content
        assert dest.exists()
        # A .dup1 file should have been created
        dup = dest.parent / "movie.dup1.mkv"
        assert dup.exists()

    def test_moves_sibling_files(self, executor, tmp_path):
        src_dir = tmp_path / "source"
        src_dir.mkdir()
        src = src_dir / "movie.mkv"
        src.write_bytes(b"\x00" * 1024)
        srt = src_dir / "movie.en.srt"
        srt.write_text("subtitle content")

        dest = tmp_path / "dest" / "Movie (2020).mkv"
        srt_dest = tmp_path / "dest" / "Movie (2020).en.srt"

        plan = make_plan(src, dest, siblings=[(srt, srt_dest)])
        results = executor.execute_plan([plan], run_id="test-run-6")

        assert results[0].success
        assert dest.exists()
        assert srt_dest.exists()

    def test_only_executes_ready_plans(self, executor, tmp_path):
        src = tmp_path / "movie.mkv"
        src.write_bytes(b"\x00" * 100)
        dest = tmp_path / "dest.mkv"

        plan = make_plan(src, dest)
        plan.status = "low_confidence"  # not ready

        results = executor.execute_plan([plan], run_id="test-run-7")
        assert len(results) == 0
        assert src.exists()  # not moved


class TestRollback:
    def test_rollback_reverses_move(self, executor, tmp_path):
        src = tmp_path / "source" / "movie.mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"\x00" * 1024)

        dest = tmp_path / "dest" / "movie.mkv"

        plan = make_plan(src, dest)
        executor.execute_plan([plan], run_id="rollback-test-1")

        assert dest.exists()
        assert not src.exists()

        # Rollback
        count = executor.rollback_run("rollback-test-1")

        assert count == 1
        assert src.exists()
        assert not dest.exists()

    def test_rollback_nonexistent_run(self, executor):
        count = executor.rollback_run("nonexistent-run-id")
        assert count == 0

    def test_rollback_already_rolled_back(self, executor, tmp_path):
        src = tmp_path / "source" / "movie.mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"\x00" * 1024)

        dest = tmp_path / "dest" / "movie.mkv"

        plan = make_plan(src, dest)
        executor.execute_plan([plan], run_id="rollback-test-2")

        # First rollback
        count1 = executor.rollback_run("rollback-test-2")
        assert count1 == 1

        # Second rollback should find nothing
        count2 = executor.rollback_run("rollback-test-2")
        assert count2 == 0

    def test_removes_empty_dirs_after_move(self, executor, tmp_path):
        src = tmp_path / "source" / "subdir" / "movie.mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"\x00" * 1024)

        dest = tmp_path / "dest" / "movie.mkv"

        plan = make_plan(src, dest)
        executor.execute_plan([plan], run_id="cleanup-test")

        # Source subdir should be cleaned up
        assert not (tmp_path / "source" / "subdir").exists()
