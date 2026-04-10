"""CLI entrypoint for MediaSorter.

Commands:
  init       — Write default config.yaml
  scan       — Scan directory and show/execute organization plan
  status     — Show DB stats and pending reviews
  version    — Print version

Global flags: --config, --log-level, --verbose, --quiet, --json
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from mediasorter import __version__

app = typer.Typer(
    name="mediasorter",
    help="Organize Jellyfin media libraries using TMDB metadata.",
    no_args_is_help=True,
)

console = Console()


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------


def _load_config(config_path: Path | None, overrides: dict | None = None):
    """Load config with optional overrides."""
    from mediasorter.config import load_config

    cfg = load_config(config_path)

    if overrides:
        # Apply CLI overrides
        data = cfg.model_dump()
        for key, value in overrides.items():
            parts = key.split(".")
            d = data
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = value

        from mediasorter.config import AppConfig

        cfg = AppConfig.model_validate(data)

    return cfg


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.callback()
def main():
    """MediaSorter — organize Jellyfin media libraries using TMDB metadata."""


@app.command()
def version():
    """Print version and exit."""
    typer.echo(f"mediasorter {__version__}")


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config"),
):
    """Write default config.yaml to ~/.config/mediasorter/config.yaml."""
    from mediasorter.config import write_default_config

    dest = Path.home() / ".config" / "mediasorter" / "config.yaml"
    try:
        path = write_default_config(dest, force=force)
        console.print(f"[green]Config written to {path}[/green]")
        console.print("Edit it to add your API keys and paths, then run [bold]mediasorter scan[/bold].")
    except FileExistsError:
        console.print(f"[yellow]Config already exists at {dest}[/yellow]")
        console.print("Use [bold]--force[/bold] to overwrite.")
        raise typer.Exit(1)


@app.command()
def scan(
    root: Annotated[Path, typer.Argument(help="Root directory to scan for media files")],
    apply: bool = typer.Option(False, "--apply", help="Actually move files (default: dry-run)"),
    media_type: str = typer.Option("both", "--type", "-t", help="Filter: movie, tv, or both"),
    since: Optional[str] = typer.Option(None, "--since", help="Only scan files modified since DATE (YYYY-MM-DD)"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config file"),
    log_level: str = typer.Option("INFO", "--log-level", "-l", help="Log level"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Shortcut for DEBUG logging"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress all but errors"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt for --apply"),
    confidence_threshold: Optional[float] = typer.Option(
        None, "--confidence-threshold", help="Override matching confidence threshold"
    ),
):
    """Scan a directory and plan/execute media organization."""
    from datetime import datetime

    from mediasorter.config import load_config
    from mediasorter.db.engine import create_tables, get_engine
    from mediasorter.logging import bind_run_id, configure_logging
    from mediasorter.moving.planner import ScanPlanner, render_plan_json, render_plan_table
    from mediasorter.utils.fs import check_mount

    # Configure logging
    level = "DEBUG" if verbose else ("ERROR" if quiet else log_level)
    configure_logging(level=level, json_output=json_output)

    # Load config
    cfg = load_config(config)

    # Apply CLI overrides
    if confidence_threshold is not None:
        cfg.matching.confidence_threshold = confidence_threshold

    # Normalize media_type filter
    type_filter = "both"
    if media_type in ("movie", "movies"):
        type_filter = "movie"
    elif media_type in ("tv", "episode", "episodes", "show", "shows"):
        type_filter = "episode"

    # Generate run ID
    run_id = str(uuid.uuid4())
    bind_run_id(run_id)

    # Validate root
    if not root.exists() or not root.is_dir():
        console.print(f"[red]Error: {root} does not exist or is not a directory[/red]")
        raise typer.Exit(1)

    # Parse --since
    since_dt = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            console.print("[red]Error: --since must be YYYY-MM-DD format[/red]")
            raise typer.Exit(1)

    # Initialize DB and planner
    engine = get_engine()
    create_tables(engine)
    planner = ScanPlanner(cfg, engine=engine)

    # Scan
    if not json_output:
        console.print(f"[bold]Scanning {root}...[/bold] (run_id: {run_id[:8]})")

    plans = planner.scan_directory(root, media_type=type_filter, since=since_dt)

    # Output
    if json_output:
        typer.echo(render_plan_json(plans))
    else:
        render_plan_table(plans, console)

    # Persist plan to DB
    planner.persist_plan(plans, run_id)

    # Apply if requested
    ready_count = sum(1 for p in plans if p.status == "ready")
    if apply and ready_count > 0:
        if not yes:
            confirm = typer.confirm(f"\nMove {ready_count} files?")
            if not confirm:
                console.print("[yellow]Aborted.[/yellow]")
                raise typer.Exit(0)

        from mediasorter.moving.executor import MoveExecutor

        executor = MoveExecutor(engine=engine, config=cfg.moving, jellyfin_config=cfg.jellyfin)
        results = executor.execute_plan(
            [p for p in plans if p.status == "ready"],
            run_id=run_id,
        )

        moved = sum(1 for r in results if r.success)
        failed = sum(1 for r in results if not r.success)

        if not json_output:
            console.print(f"\n[bold]Done:[/bold] {moved} moved, {failed} failed")
    elif apply and ready_count == 0:
        if not json_output:
            console.print("\n[dim]No files ready to move.[/dim]")


@app.command()
def status(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show DB stats and pending reviews."""
    from sqlmodel import Session, func, select

    from mediasorter.db.engine import create_tables, get_engine
    from mediasorter.db.models import MediaFile, MoveLog, RunLog, TMDBMatch

    engine = get_engine()
    create_tables(engine)

    with Session(engine) as session:
        total_files = session.exec(select(func.count(MediaFile.id))).one()
        total_moves = session.exec(select(func.count(MoveLog.id))).one()
        pending_review = session.exec(
            select(func.count(TMDBMatch.id)).where(TMDBMatch.confidence < 0.85)
        ).one()

        console.print(f"[bold]MediaSorter Status[/bold]")
        console.print(f"  Known files:      {total_files}")
        console.print(f"  Total moves:      {total_moves}")
        console.print(f"  Pending review:   {pending_review}")


@app.command()
def rollback(
    run_id: Annotated[str, typer.Argument(help="Run ID to rollback")],
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Reverse all moves from a specific run."""
    from mediasorter.config import load_config
    from mediasorter.db.engine import create_tables, get_engine
    from mediasorter.moving.executor import MoveExecutor

    cfg = load_config(config)
    engine = get_engine()
    create_tables(engine)

    executor = MoveExecutor(engine=engine, config=cfg.moving, jellyfin_config=cfg.jellyfin)
    count = executor.rollback_run(run_id)

    if count > 0:
        console.print(f"[green]Rolled back {count} moves from run {run_id[:8]}[/green]")
    else:
        console.print(f"[yellow]No moves found for run {run_id}[/yellow]")
