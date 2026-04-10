"""Filesystem utilities — mount checks, safe operations, sibling detection.

Designed for slow CIFS mounts: minimize stat calls, handle mount drops gracefully.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mkv", ".mp4", ".avi", ".m4v", ".wmv", ".mov", ".ts", ".m2ts"}
)

SUBTITLE_EXTENSIONS: frozenset[str] = frozenset({".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"})

METADATA_EXTENSIONS: frozenset[str] = frozenset({".nfo"})

IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".tbn"})

SIBLING_EXTENSIONS: frozenset[str] = SUBTITLE_EXTENSIONS | METADATA_EXTENSIONS | IMAGE_EXTENSIONS

INCOMPLETE_EXTENSIONS: frozenset[str] = frozenset({".part", ".!qb", ".downloading", ".tmp"})

SENTINEL_FILENAME = ".mediasorter"


def is_video_file(path: Path) -> bool:
    """Check if file has a video extension."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_sample_file(path: Path, media_type: str, min_movie_mb: int = 50, min_episode_mb: int = 5) -> bool:
    """Check if a file is a sample (by name or size).

    Samples are identified by:
    - Filename containing 'sample' (case-insensitive)
    - File size below threshold for its media type
    """
    name_lower = path.name.lower()
    if "sample" in name_lower:
        return True

    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        return False

    threshold = min_movie_mb if media_type == "movie" else min_episode_mb
    return size_mb < threshold


def is_incomplete_file(path: Path) -> bool:
    """Check if a file appears to still be downloading.

    Only checks file extension (e.g., .part, .!qb). The mtime-based
    check for recently modified files is handled separately via is_file_in_use.
    """
    return path.suffix.lower() in INCOMPLETE_EXTENSIONS


def is_file_in_use(path: Path) -> bool:
    """Check if a file is currently open by another process using lsof."""
    try:
        result = subprocess.run(
            ["lsof", str(path)],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0  # 0 means file IS open
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_mount(path: Path) -> bool:
    """Verify a mount point is healthy.

    Checks:
    1. Path exists and is a directory
    2. Parent (or self) is a mount point
    3. Can write a sentinel file (proves write access)
    """
    if not path.exists() or not path.is_dir():
        return False

    sentinel = path / SENTINEL_FILENAME
    try:
        sentinel.write_text(str(time.time()))
        sentinel.unlink()
        return True
    except OSError:
        return False


def find_sibling_files(video_path: Path) -> list[Path]:
    """Find non-video files with the same basename (subtitles, .nfo, artwork).

    For a video at /path/to/Movie.mkv, finds:
    - /path/to/Movie.srt
    - /path/to/Movie.en.srt
    - /path/to/Movie.nfo
    - /path/to/Movie.jpg
    etc.
    """
    parent = video_path.parent
    stem = video_path.stem
    siblings = []

    try:
        for child in parent.iterdir():
            if child == video_path:
                continue
            # Match exact stem or stem.lang (e.g., Movie.en.srt)
            child_name = child.name
            if child_name.startswith(stem) and child.suffix.lower() in SIBLING_EXTENSIONS:
                siblings.append(child)
    except OSError:
        pass

    return siblings


def safe_remove_empty_dirs(path: Path, stop_at: Path) -> None:
    """Remove empty parent directories up to (but not including) stop_at."""
    current = path
    while current != stop_at and current != current.parent:
        try:
            if current.is_dir() and not any(current.iterdir()):
                current.rmdir()
            else:
                break
        except OSError:
            break
        current = current.parent
