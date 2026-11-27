"""Where TuneBridge keeps its local data. Nothing is ever sent anywhere else."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP = "tunebridge"


def data_dir() -> Path:
    p = Path(os.environ.get("TUNEBRIDGE_HOME") or user_data_dir(APP))
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    p = Path(os.environ.get("TUNEBRIDGE_HOME") or user_config_dir(APP))
    p.mkdir(parents=True, exist_ok=True)
    return p


def profile_dir(platform: str) -> Path:
    """Dedicated browser profile per platform. Holds the login session (cookies) only."""
    return data_dir() / "profiles" / platform


def reports_dir() -> Path:
    p = data_dir() / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def exports_dir() -> Path:
    p = Path(os.environ.get("TUNEBRIDGE_EXPORTS") or (Path.home() / "Music" / "tunebridge"))
    return p
