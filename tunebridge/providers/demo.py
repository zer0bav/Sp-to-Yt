"""Offline demo platforms (enabled with TUNEBRIDGE_DEMO=1). Used for UI development and tests."""

from __future__ import annotations

import os
import random
import time

from ..models import Playlist, Track
from .base import Progress, Provider, register

_DELAY = float(os.environ.get("TUNEBRIDGE_DEMO_DELAY", "0.05"))

_LIBRARY: dict[str, tuple[str, list[tuple[str, str, int]]]] = {
    "p1": ("Late Night Drive", [
        ("Midnight City", "M83", 244), ("Nightcall", "Kavinsky", 258), ("Blinding Lights", "The Weeknd", 200),
        ("Instant Crush", "Daft Punk, Julian Casablancas", 337), ("Redbone", "Childish Gambino", 327),
        ("The Less I Know The Better", "Tame Impala", 216), ("Motion Sickness", "Phoebe Bridgers", 229),
        ("Gece Mavisi", "Duman", 241), ("Unknown Demo Track", "Nobody In Particular", 180),
    ]),
    "p2": ("Focus", [
        ("Intro", "The xx", 128), ("Weightless", "Marconi Union", 480), ("Nuvole Bianche", "Ludovico Einaudi", 357),
        ("An Ending (Ascent)", "Brian Eno", 266), ("Avril 14th", "Aphex Twin", 125),
    ]),
    "p3": ("Türkçe Rock", [
        ("Bu Akşam", "Duman", 220), ("Islak Islak", "Cem Karaca", 286), ("Yalnızlık Senfonisi", "Manga", 254),
        ("Senden Daha Güzel", "Duman", 233), ("Hayvan Gibi Sevişmek", "Mor ve Ötesi", 230),
    ]),
}


@register
class DemoProvider(Provider):
    name = "demo"
    label = "Demo Music"
    uses_browser = False
    note = "Offline sample library."

    def __init__(self, **kw):
        super().__init__(**kw)
        self.created: dict[str, Playlist] = {}
        self.contents: dict[str, list[Track]] = {}

    def is_logged_in(self) -> bool:
        return True

    def list_playlists(self) -> list[Playlist]:
        out = [Playlist(id=k, name=v[0], track_count=len(v[1]), owner="you") for k, v in _LIBRARY.items()]
        out.append(Playlist(id="liked", name="Liked Songs", track_count=3, kind="liked", editable=False))
        return out + list(self.created.values())

    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]:
        if playlist.id in self.contents:
            return list(self.contents[playlist.id])
        items = _LIBRARY.get(playlist.id, ("", [("Blinding Lights", "The Weeknd", 200)] * 3))[1]
        out = []
        for i, (t, a, d) in enumerate(items):
            time.sleep(_DELAY)
            out.append(Track(title=t, artists=[x.strip() for x in a.split(",")], duration=d, id=f"{playlist.id}-{i}"))
            if progress:
                progress(i + 1, len(items))
        return out

    def search(self, track: Track, limit: int = 6) -> list[Track]:
        time.sleep(_DELAY * 2)
        if "Unknown" in track.title:
            return [Track(title="Something Else", artists=["Other Artist"], duration=150, id="x1")]
        rnd = random.Random(track.title)
        main = Track(title=track.title, artists=list(track.artists), duration=track.duration, id=f"m-{abs(hash(track.title)) % 10**8}")
        if rnd.random() < 0.2:  # simulate a fuzzy result that needs review
            main = Track(title=f"{track.title} (Live)", artists=list(track.artists), duration=(track.duration or 200) + 25, id=main.id)
        alt = Track(title=f"{track.title} (Remix)", artists=["DJ Someone"], duration=(track.duration or 200) + 60, id=main.id + "r")
        return [main, alt][:limit]

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        pid = f"new{len(self.created) + 1}"
        pl = Playlist(id=pid, name=name, track_count=0)
        self.created[pid] = pl
        self.contents[pid] = []
        return pl

    def add_tracks(self, playlist: Playlist, tracks: list[Track], progress: Progress | None = None) -> list[Track]:
        for i, t in enumerate(tracks):
            time.sleep(_DELAY)
            self.contents.setdefault(playlist.id, []).append(t)
            if progress:
                progress(i + 1, len(tracks))
        return list(tracks)


@register
class DemoTargetProvider(DemoProvider):
    name = "demo2"
    label = "Demo Stream"
    note = "Offline target for trying the UI."

    def list_playlists(self) -> list[Playlist]:
        return list(self.created.values())
