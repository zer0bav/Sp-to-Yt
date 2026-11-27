"""Local SQLite memory of what has been moved, so re-runs only add new songs."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlist_map (
    source TEXT NOT NULL, source_pl TEXT NOT NULL, target TEXT NOT NULL,
    target_pl TEXT NOT NULL, target_name TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_pl, target)
);
CREATE TABLE IF NOT EXISTS track_map (
    source TEXT NOT NULL, source_pl TEXT NOT NULL, target TEXT NOT NULL,
    track_key TEXT NOT NULL, target_id TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_pl, target, track_key)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "state.db")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def target_playlist(self, source: str, source_pl: str, target: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT target_pl, target_name FROM playlist_map WHERE source=? AND source_pl=? AND target=?",
                (source, source_pl, target),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def set_target_playlist(self, source: str, source_pl: str, target: str, target_pl: str, target_name: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO playlist_map VALUES (?,?,?,?,?,?)",
                (source, source_pl, target, target_pl, target_name, _now()),
            )

    def added_keys(self, source: str, source_pl: str, target: str) -> set[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT track_key FROM track_map WHERE source=? AND source_pl=? AND target=?",
                (source, source_pl, target),
            ).fetchall()
        return {r[0] for r in rows}

    def mark_added(self, source: str, source_pl: str, target: str, items: list[tuple[str, str | None]]) -> None:
        now = _now()
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO track_map VALUES (?,?,?,?,?,?)",
                [(source, source_pl, target, key, tid, now) for key, tid in items],
            )

    def forget(self, source: str, source_pl: str, target: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM playlist_map WHERE source=? AND source_pl=? AND target=?", (source, source_pl, target))
            self._db.execute("DELETE FROM track_map WHERE source=? AND source_pl=? AND target=?", (source, source_pl, target))
