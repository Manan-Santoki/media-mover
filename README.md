# MediaSorter

Organize a messy Jellyfin media library into a clean, canonical folder structure using TMDB metadata, with OpenRouter AI fallback and webhook notifications.

## Features

- **Filename parsing** — Uses [guessit](https://github.com/guessit-io/guessit) to extract title, year, season, episode from messy filenames
- **TMDB matching** — Searches TMDB for canonical metadata with cached results and confidence scoring
- **AI fallback** — When TMDB matching fails, falls back to OpenRouter (configurable model) for identification
- **Dry-run by default** — Shows a plan of what would move before touching any files
- **Rollback** — Every move is logged and reversible by run ID
- **Sibling files** — Moves subtitles, .nfo, artwork alongside their video files
- **Upcoming episodes** — Tracks when new episodes air for shows in your library
- **Webhook notifications** — Sends events to Pickrr or any webhook endpoint
- **Daemon mode** — Runs on a schedule with health monitoring
- **Docker ready** — Includes Dockerfile and docker-compose.yml

## Target Folder Structure

### TV Shows
```
Shows/
├── Breaking Bad (2008) [tmdbid-1396]/
│   ├── Season 01/
│   │   ├── Breaking Bad - S01E01 - Pilot.mkv
│   │   └── Breaking Bad - S01E02 - Cat's in the Bag.mkv
│   └── Season 02/
│       └── ...
```

### Movies
```
Movies/
├── The Matrix (1999) [imdbid-tt0133093]/
│   ├── The Matrix (1999).mkv
│   ├── The Matrix (1999).en.srt
│   └── The Matrix (1999).nfo
```

## Installation

### From source (recommended for development)

```bash
git clone https://github.com/yourusername/media-mover.git
cd media-mover
pip install -e ".[dev]"
```

### With uv

```bash
uv tool install mediasorter
```

### Docker

```bash
docker compose up -d
```

## Quick Start

```bash
# 1. Generate a default config file
mediasorter init

# 2. Edit the config with your API keys and paths
#    ~/.config/mediasorter/config.yaml

# 3. Scan your library (dry-run)
mediasorter scan /path/to/media

# 4. Review the plan, then apply
mediasorter scan /path/to/media --apply --yes

# 5. Check for upcoming episodes
mediasorter check-upcoming --notify
```

## CLI Commands

```
mediasorter init [--force]                    # Write default config.yaml
mediasorter config show                       # Print resolved config
mediasorter config validate                   # Check config + API keys
mediasorter scan ROOT [--apply] [--type TYPE] # Scan and plan/execute moves
mediasorter organize [--apply]                # Scan all configured roots
mediasorter check-upcoming [--notify]         # Check for upcoming episodes
mediasorter rollback RUN_ID                   # Reverse a previous run
mediasorter status                            # Show DB stats
mediasorter review                            # Interactive TUI for low-confidence matches
mediasorter daemon                            # Long-running mode with scheduler
mediasorter version                           # Print version
```

### Global flags
```
--config PATH          Use a specific config file
--log-level LEVEL      Override config log level
--verbose / -v         DEBUG logging
--quiet / -q           Errors only
--json                 Machine-readable output
```

## Configuration

MediaSorter uses a YAML config file with `${VAR}` environment variable interpolation.

**Config discovery order:**
1. `$MEDIASORTER_CONFIG` env var
2. `--config PATH` flag
3. `./mediasorter.yaml`
4. `~/.config/mediasorter/config.yaml`

See [config.example.yaml](config.example.yaml) for all options.

### Key settings

```yaml
roots:
  shows: /mnt/storagebox/jellyfin/media/Shows
  movies: /mnt/storagebox/jellyfin/media/Movies

tmdb:
  api_key: ${TMDB_API_KEY}
  cache_ttl_days: 30

matching:
  confidence_threshold: 0.85    # Below this, flags for review

moving:
  apply: false                  # Set true for daemon auto-move

daemon:
  organize_cron: "0 */6 * * *"  # Every 6 hours
  upcoming_cron: "0 8 * * *"    # Daily at 8am
  health_port: 9876
```

## Architecture

```
src/mediasorter/
├── cli.py              # Typer CLI
├── config.py           # YAML config with pydantic validation
├── db/                 # SQLite (SQLModel) for state, cache, audit log
├── parsing/            # GuessIt wrapper + filename normalization
├── matching/           # TMDB client, confidence scorer, AI fallback
├── moving/             # Move planner (dry-run) + executor (with rollback)
├── notifications/      # Upcoming episode tracker + webhook dispatcher
├── daemon/             # APScheduler + /health endpoint
├── tui/                # Rich-based interactive review
└── utils/              # Filesystem ops, rate limiting
```

### Pipeline

```
file → is_video? → parse (guessit) → search TMDB → score confidence
  → build canonical path → check duplicates → MovePlan
  → [--apply] → move + log → clean empty dirs → refresh Jellyfin
```

## Requirements

- Python 3.11+
- TMDB API key ([get one here](https://www.themoviedb.org/settings/api))
- OpenRouter API key (optional, for AI fallback)

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=mediasorter

# Lint
ruff check src/ tests/
```

## License

MIT
