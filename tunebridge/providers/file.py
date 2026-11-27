"""CSV / JSON files: export any platform for backup, or import from a file into any platform."""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Playlist, Track
from ..paths import exports_dir
from .base import Progress, Provider, register

CSV_FIELDS = ["title", "artists", "album", "duration", "isrc", "id", "url"]


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\-. ]+", "", name, flags=re.U).strip().replace(" ", "_")
    return s or "playlist"


@register
class FileProvider(Provider):
    name = "file"
    label = "CSV / JSON files"
    uses_browser = False
    exact_copy = True
    note = "Reads and writes playlist files in a folder (default ~/Music/tunebridge)."

    def __init__(self, *, directory: str | os.PathLike | None = None, fmt: str = "json", **kw: Any) -> None:
        super().__init__(**kw)
        self.dir = Path(directory) if directory else exports_dir()
        self.fmt = fmt if fmt in ("json", "csv") else "json"

    def is_logged_in(self) -> bool:
        return True

    def list_playlists(self) -> list[Playlist]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.iterdir()):
            if p.suffix.lower() not in (".json", ".csv") or not p.is_file():
                continue
            try:
                name, tracks = self._read(p)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            out.append(Playlist(id=str(p), name=name, track_count=len(tracks), url=str(p), kind="file"))
        return out

    def _read(self, path: Path) -> tuple[str, list[Track]]:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return path.stem, [Track.from_dict(t) for t in data]
            return data.get("name") or path.stem, [Track.from_dict(t) for t in data.get("tracks", [])]
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        return path.stem.replace("_", " "), [Track.from_dict({k.lower(): v for k, v in r.items() if k}) for r in rows]

    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]:
        _, tracks = self._read(Path(playlist.id))
        tracks = [t for t in tracks if t.title]
        if progress:
            progress(len(tracks), len(tracks))
        return tracks

    def search(self, track: Track, limit: int = 6) -> list[Track]:
        return [track]

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{_slug(name)}.{self.fmt}"
        self._write(path, name, [], description)
        return Playlist(id=str(path), name=name, track_count=0, url=str(path), kind="file")

    def _write(self, path: Path, name: str, tracks: list[Track], description: str = "") -> None:
        if path.suffix == ".json":
            payload = {
                "name": name,
                "description": description,
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "generator": "tunebridge",
                "tracks": [t.to_dict() for t in tracks],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                w.writeheader()
                for t in tracks:
                    d = t.to_dict()
                    d["artists"] = "; ".join(t.artists)
                    w.writerow({k: d.get(k) or "" for k in CSV_FIELDS})

    def add_tracks(self, playlist: Playlist, tracks: list[Track], progress: Progress | None = None) -> list[Track]:
        path = Path(playlist.id)
        name, existing = self._read(path) if path.exists() else (playlist.name, [])
        description = ""
        if path.suffix == ".json" and path.exists():
            description = json.loads(path.read_text(encoding="utf-8")).get("description", "")
        self._write(path, name, existing + tracks, description)
        if progress:
            progress(len(tracks), len(tracks))
        return list(tracks)
