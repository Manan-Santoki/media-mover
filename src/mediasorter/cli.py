"""CLI entrypoint for MediaSorter."""

import typer

app = typer.Typer(
    name="mediasorter",
    help="Organize Jellyfin media libraries using TMDB metadata",
    no_args_is_help=True,
)


@app.callback()
def main():
    """MediaSorter — organize Jellyfin media libraries using TMDB metadata."""


@app.command()
def version():
    """Print version and exit."""
    from mediasorter import __version__

    typer.echo(f"mediasorter {__version__}")


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite existing config")):
    """Write default config.yaml to ~/.config/mediasorter/config.yaml."""
    typer.echo("init (not yet implemented)")
