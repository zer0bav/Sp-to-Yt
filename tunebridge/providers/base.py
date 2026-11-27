"""Provider interface. One class per platform; see CONTRIBUTING.md to add a new one."""

from __future__ import annotations

import time
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, ClassVar

from ..browser import Browser, BrowserError, has_profile
from ..models import Playlist, Track

Progress = Callable[[int, int | None], None]  # (done, total or None)

_SELECTOR_DIR = Path(__file__).with_name("selectors")


@cache
def load_selectors(name: str) -> dict[str, Any]:
    """Selectors live in providers/selectors/<name>.toml so a site redesign is a one-file fix."""
    path = _SELECTOR_DIR / f"{name}.toml"
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


class NotLoggedIn(BrowserError):
    pass


class Provider(ABC):
    #: short id used on the command line: "spotify", "ytmusic", ...
    name: ClassVar[str]
    #: human name shown in the UIs
    label: ClassVar[str]
    home_url: ClassVar[str] = ""
    login_url: ClassVar[str] = ""
    #: False when the site refuses to work in a headless browser; a window is shown instead
    headless_ok: ClassVar[bool] = True
    #: selectors not yet verified against a live account
    experimental: ClassVar[bool] = False
    #: needs a browser at all (the file provider does not)
    uses_browser: ClassVar[bool] = True
    #: when True the engine copies tracks verbatim instead of searching (file export)
    exact_copy: ClassVar[bool] = False
    note: ClassVar[str] = ""

    def __init__(self, *, show_browser: bool = False, **_: Any) -> None:
        self.show_browser = show_browser
        self._browser: Browser | None = None
        self.sel = load_selectors(self.name)

    # ---- browser lifecycle -------------------------------------------------------

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            headless = self.headless_ok and not self.show_browser
            self._browser = Browser(self.name, headless=headless)
        return self._browser

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def __enter__(self) -> Provider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @classmethod
    def has_session(cls) -> bool:
        """Cheap check: has the user ever logged in (profile exists)? Does not open a browser."""
        return not cls.uses_browser or has_profile(cls.name)

    def require_login(self) -> None:
        if not self.is_logged_in():
            raise NotLoggedIn(f"Not logged in to {self.label}. Run: tunebridge login {self.name}")

    # ---- the contract ------------------------------------------------------------

    @abstractmethod
    def is_logged_in(self) -> bool: ...

    @abstractmethod
    def list_playlists(self) -> list[Playlist]: ...

    @abstractmethod
    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]: ...

    @abstractmethod
    def search(self, track: Track, limit: int = 6) -> list[Track]: ...

    @abstractmethod
    def create_playlist(self, name: str, description: str = "") -> Playlist: ...

    @abstractmethod
    def add_tracks(self, playlist: Playlist, tracks: list[Track], progress: Progress | None = None) -> list[Track]:
        """Add tracks (previously returned by `search`) and return the ones that were added."""

    def find_playlist(self, name: str) -> Playlist | None:
        want = name.strip().lower()
        for p in self.list_playlists():
            if p.name.strip().lower() == want and p.editable:
                return p
        return None

    # ---- shared scraping helpers -------------------------------------------------

    def collect_scrolling(
        self,
        collect_js: str,
        scroll_js: str,
        *,
        args: tuple[Any, ...] = (),
        expected: int | None = None,
        progress: Progress | None = None,
        pause: float = 0.7,
        max_idle: int = 4,
        max_rounds: int = 2000,
    ) -> list[dict[str, Any]]:
        """Scrape a virtualised list: collect visible rows, scroll, repeat until nothing new shows up.

        Both scripts receive `args` as `arguments`. `collect_js` must return a list of objects with a
        unique `k` field.
        """
        seen: dict[str, dict[str, Any]] = {}
        idle = 0
        for _ in range(max_rounds):
            rows = self.browser.js(collect_js, *args) or []
            before = len(seen)
            for r in rows:
                if r and r.get("k") is not None and r["k"] not in seen:
                    seen[r["k"]] = r
            if progress:
                progress(len(seen), expected)
            if expected is not None and len(seen) >= expected:
                break
            idle = idle + 1 if len(seen) == before else 0
            # rows often arrive over the network after a scroll; be patient while we know more exist
            patience = max_idle * 4 if expected is not None and len(seen) < expected else max_idle
            if idle >= patience:
                break
            self.browser.js(scroll_js, *args)
            time.sleep(pause)
        return list(seen.values())


Factory = type[Provider]
_REGISTRY: dict[str, Factory] = {}


def register(cls: Factory) -> Factory:
    _REGISTRY[cls.name] = cls
    return cls


def registry() -> dict[str, Factory]:
    from . import _load_all  # noqa: F401  (imports every provider module once)

    return dict(_REGISTRY)


def get_provider(name: str, **kwargs: Any) -> Provider:
    reg = registry()
    if name not in reg:
        raise KeyError(f"Unknown platform '{name}'. Available: {', '.join(sorted(reg))}")
    return reg[name](**kwargs)
