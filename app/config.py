"""Application settings, read from the environment."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = "data/ammonia.db"


def resolve_db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.getenv("AMMONIA_DB_PATH", DEFAULT_DB_PATH))
