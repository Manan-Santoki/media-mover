"""Rich-based interactive TUI for reviewing low-confidence matches.

Presents each low-confidence match to the user and allows them to:
- Accept the suggested match
- Skip (leave for later)
- Manually enter a TMDB ID
"""

from __future__ import annotations

from sqlmodel import Session, select

import structlog
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from mediasorter.db.models import TMDBMatch

log = structlog.get_logger(__name__)


def review_matches(engine, threshold: float = 0.85) -> int:
    """Interactive review of low-confidence matches.

    Returns number of matches reviewed.
    """
    console = Console()

    with Session(engine) as session:
        stmt = (
            select(TMDBMatch)
            .where(TMDBMatch.confidence < threshold)
            .order_by(TMDBMatch.confidence.asc())
        )
        matches = session.exec(stmt).all()

        if not matches:
            console.print("[dim]No matches pending review.[/dim]")
            return 0

        console.print(f"[bold]{len(matches)} matches to review[/bold]\n")

        reviewed = 0
        for i, match in enumerate(matches, 1):
            console.print(
                Panel(
                    f"[bold]Match {i}/{len(matches)}[/bold]\n\n"
                    f"  TMDB ID:     {match.tmdb_id}\n"
                    f"  Title:       {match.matched_title}\n"
                    f"  Year:        {match.matched_year}\n"
                    f"  Type:        {match.tmdb_type}\n"
                    f"  Confidence:  {match.confidence:.2f}\n"
                    f"  Source:      {match.match_source}\n"
                    f"  Dest path:   {match.dest_path}",
                    title="Low Confidence Match",
                )
            )

            choice = Prompt.ask(
                "Action",
                choices=["accept", "skip", "manual", "quit"],
                default="skip",
            )

            if choice == "accept":
                match.confidence = 1.0
                match.match_source = "manual"
                session.add(match)
                session.commit()
                console.print("[green]Accepted.[/green]\n")
                reviewed += 1
            elif choice == "manual":
                tmdb_id = Prompt.ask("Enter correct TMDB ID")
                try:
                    match.tmdb_id = int(tmdb_id)
                    match.confidence = 1.0
                    match.match_source = "manual"
                    session.add(match)
                    session.commit()
                    console.print(f"[green]Updated to TMDB ID {tmdb_id}.[/green]\n")
                    reviewed += 1
                except ValueError:
                    console.print("[red]Invalid TMDB ID, skipping.[/red]\n")
            elif choice == "quit":
                break
            else:
                console.print("[dim]Skipped.[/dim]\n")

        console.print(f"\n[bold]Reviewed {reviewed} matches.[/bold]")
        return reviewed
