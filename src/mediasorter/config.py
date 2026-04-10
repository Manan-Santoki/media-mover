"""Configuration loading and validation.

Loads a YAML config file with ${VAR} environment variable interpolation.
Discovery order: $MEDIASORTER_CONFIG -> --config flag -> ./mediasorter.yaml
-> ~/.config/mediasorter/config.yaml -> error with helpful message.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RootsConfig(BaseModel):
    shows: Path = Path("/mnt/storagebox/jellyfin/media/Shows")
    movies: Path = Path("/mnt/storagebox/jellyfin/media/Movies")


class TMDBConfig(BaseModel):
    api_key: str = ""
    language: str = "en-US"
    cache_ttl_days: int = 30


class OpenRouterConfig(BaseModel):
    api_key: str = ""
    model: str = "google/gemini-flash-1.5"
    max_cost_per_run_usd: float = 1.00
    enabled: bool = False


class MatchingConfig(BaseModel):
    confidence_threshold: float = 0.85
    min_movie_size_mb: int = 50
    min_episode_size_mb: int = 5


class MovingConfig(BaseModel):
    apply: bool = False
    trash_dir: Path = Path("/mnt/storagebox/jellyfin/.mediasorter-trash")
    trash_ttl_days: int = 14


class JellyfinConfig(BaseModel):
    url: str = ""
    api_key: str = ""


class WebhookEndpoint(BaseModel):
    url: str = ""
    secret: str = ""
    events: list[str] = Field(
        default_factory=lambda: ["upcoming_episode", "scan_complete", "files_moved", "error"]
    )


class WebhooksConfig(BaseModel):
    pickrr: WebhookEndpoint = Field(default_factory=WebhookEndpoint)


class NotificationsConfig(BaseModel):
    window_days: int = 3


class DaemonConfig(BaseModel):
    organize_cron: str = "0 */6 * * *"
    upcoming_cron: str = "0 8 * * *"
    health_port: int = 9876


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


class AppConfig(BaseModel):
    roots: RootsConfig = Field(default_factory=RootsConfig)
    tmdb: TMDBConfig = Field(default_factory=TMDBConfig)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    moving: MovingConfig = Field(default_factory=MovingConfig)
    jellyfin: JellyfinConfig = Field(default_factory=JellyfinConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def resolve_env_vars(text: str) -> str:
    """Replace ${VAR} references with environment variable values.

    Missing vars are replaced with empty string.
    """

    def _replace(match: re.Match) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_VAR_RE.sub(_replace, text)


_CONFIG_SEARCH_PATHS = [
    Path("./mediasorter.yaml"),
    Path.home() / ".config" / "mediasorter" / "config.yaml",
]


def find_config_file(explicit_path: Path | None = None) -> Path | None:
    """Find config file using the discovery order.

    Returns None if no config file is found (caller decides whether to error).
    """
    if explicit_path is not None:
        return explicit_path

    env_path = os.environ.get("MEDIASORTER_CONFIG")
    if env_path:
        return Path(env_path)

    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            return candidate

    return None


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load and validate config from YAML file.

    If no config file is found, returns default config.
    """
    resolved = find_config_file(config_path)
    if resolved is None:
        return AppConfig()

    raw = resolved.read_text()
    interpolated = resolve_env_vars(raw)
    data = yaml.safe_load(interpolated) or {}
    return AppConfig.model_validate(data)


def write_default_config(dest: Path, force: bool = False) -> Path:
    """Write a default config file with commented examples."""
    if dest.exists() and not force:
        raise FileExistsError(f"Config already exists at {dest}. Use --force to overwrite.")

    dest.parent.mkdir(parents=True, exist_ok=True)

    src = Path(__file__).parent.parent.parent / "config.example.yaml"
    if src.exists():
        dest.write_text(src.read_text())
    else:
        dest.write_text(AppConfig().model_dump_json(indent=2))

    return dest
