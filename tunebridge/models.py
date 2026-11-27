"""Plain data types shared by every provider, the engine and both UIs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "--:--"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_duration(text: str | None) -> int | None:
    """'3:45' -> 225, '1:02:03' -> 3723. Returns None when the text is not a duration."""
    if not text:
        return None
    parts = text.strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


@dataclass
class Track:
    title: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration: int | None = None  # seconds
    isrc: str | None = None
    id: str | None = None  # platform-specific id (Spotify track id, YouTube videoId, ...)
    url: str | None = None

    @property
    def artist_line(self) -> str:
        return ", ".join(self.artists)

    def key(self) -> str:
        """Stable identity used by the state store to remember what was already moved."""
        if self.id:
            return f"id:{self.id}"
        return f"t:{self.title.lower().strip()}|{self.artist_line.lower().strip()}"

    def label(self) -> str:
        return f"{self.title} - {self.artist_line}" if self.artists else self.title

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Track:
        artists = d.get("artists") or []
        if isinstance(artists, str):
            artists = [a.strip() for a in artists.split(";") if a.strip()]
        duration = d.get("duration")
        if isinstance(duration, str):
            duration = parse_duration(duration) if ":" in duration else (int(duration) if duration.isdigit() else None)
        return cls(
            title=str(d.get("title") or "").strip(),
            artists=list(artists),
            album=d.get("album") or None,
            duration=duration,
            isrc=d.get("isrc") or None,
            id=d.get("id") or None,
            url=d.get("url") or None,
        )


@dataclass
class Playlist:
    id: str
    name: str
    track_count: int | None = None
    owner: str | None = None
    url: str | None = None
    kind: str = "playlist"  # "playlist" | "liked" | "file"
    editable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Playlist:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
