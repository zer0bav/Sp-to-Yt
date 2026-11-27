"""TIDAL web player (listen.tidal.com) driven with Selenium.

TIDAL needs a login to show anything and refuses headless browsers, so this
provider runs in a visible window. Selectors are experimental; please report or
fix them in providers/selectors/tidal.toml.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys

from ..browser import BrowserError
from ..matcher import search_query
from ..models import Playlist, Track, parse_duration
from .base import Progress, Provider, register

BASE = "https://listen.tidal.com"

_ROWS_JS = r"""
const [rowSel, titleSel, artistSel, albumSel, durSel] = arguments;
const out = [];
let i = 0;
for (const row of document.querySelectorAll(rowSel)) {
  i++;
  const t = row.querySelector(titleSel);
  if (!t) continue;
  const id = row.getAttribute('data-track-id') || (row.querySelector('[data-track-id]') || {getAttribute: () => null}).getAttribute('data-track-id');
  const al = row.querySelector(albumSel);
  const d = row.querySelector(durSel);
  const idx = parseInt(row.getAttribute('aria-rowindex') || row.getAttribute('data-index') || i);
  out.push({k: id || String(idx), idx, id, title: t.innerText.split('\n')[0].trim(),
            artists: [...new Set([...row.querySelectorAll(artistSel)].map(a => a.innerText.trim()).filter(Boolean))],
            album: al ? al.innerText.trim() : null, duration: d ? d.innerText.trim() : null});
}
return out;
"""

_SCROLL_JS = r"""
const rows = document.querySelectorAll(arguments[0]);
if (rows.length) rows[rows.length - 1].scrollIntoView({block: 'end'});
"""


@register
class TidalProvider(Provider):
    name = "tidal"
    label = "TIDAL"
    home_url = BASE + "/"
    login_url = "https://login.tidal.com/"
    headless_ok = False
    experimental = True
    note = "Experimental. Runs in a visible browser window."

    def is_logged_in(self) -> bool:
        b = self.browser
        b.get(self.home_url)
        s = self.sel["session"]
        try:
            which, _ = b.wait_any([s["logged_in"], s["logged_out"]], timeout=30)
        except BrowserError:
            return False
        return which == s["logged_in"]

    def _rows(self, expected: int | None = None, progress: Progress | None = None, scroll: bool = True) -> list[Track]:
        t = self.sel["tracks"]
        args = (t["row"], t["title"], t["artist"], t["album"], t["duration"])
        rows = (self.collect_scrolling(_ROWS_JS, _SCROLL_JS, args=args, expected=expected, progress=progress)
                if scroll else (self.browser.js(_ROWS_JS, *args) or []))
        rows.sort(key=lambda r: r.get("idx") or 0)
        return [Track(title=r["title"], artists=r.get("artists") or [], album=r.get("album"),
                      duration=parse_duration(r.get("duration")), id=r.get("id"),
                      url=f"{BASE}/track/{r['id']}" if r.get("id") else None) for r in rows if r.get("title")]

    def list_playlists(self) -> list[Playlist]:
        b = self.browser
        lib = self.sel["library"]
        b.get(lib["url"])
        try:
            b.wait_css(lib["items"], timeout=30)
        except BrowserError:
            return []
        time.sleep(1.5)
        items = b.js(
            "return [...document.querySelectorAll(arguments[0])].map(a => ({href: a.href, name: (a.getAttribute('aria-label') || a.innerText || '').trim()}))",
            lib["items"],
        ) or []
        out: dict[str, Playlist] = {}
        for it in items:
            m = re.search(r"/playlist/([0-9a-f-]{8,})", it.get("href") or "")
            if m and it.get("name") and m.group(1) not in out:
                out[m.group(1)] = Playlist(id=m.group(1), name=it["name"].split("\n")[0], url=f"{BASE}/playlist/{m.group(1)}")
        return list(out.values())

    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]:
        b = self.browser
        b.get(playlist.url or f"{BASE}/playlist/{playlist.id}")
        try:
            b.wait_css(self.sel["tracks"]["row"], timeout=30)
        except BrowserError:
            return []
        time.sleep(1.0)
        return self._rows(playlist.track_count, progress)

    def search(self, track: Track, limit: int = 6) -> list[Track]:
        b = self.browser
        b.get(f"{BASE}/search/tracks?q={quote(search_query(track))}")
        try:
            b.wait_css(self.sel["tracks"]["row"], timeout=20)
        except BrowserError:
            return []
        time.sleep(0.8)
        return [t for t in self._rows(scroll=False)[:limit] if t.id]

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        b = self.browser
        c = self.sel["create"]
        b.get(self.sel["library"]["url"])
        time.sleep(2.0)
        for text in c["open_texts"]:
            try:
                b.click_text(c["buttons"], text, timeout=6)
                break
            except BrowserError:
                continue
        else:
            raise BrowserError("Could not find TIDAL's 'Create playlist' button.")
        inp = b.wait_css(c["title_input"], timeout=10, visible=True)
        inp.send_keys(name)
        for text in c["confirm_texts"]:
            try:
                b.click_text("button", text, timeout=4, exact=True)
                break
            except BrowserError:
                continue
        time.sleep(2.5)
        found = self.find_playlist(name)
        if not found:
            raise BrowserError("Created the playlist but could not find it.")
        return found

    def add_tracks(self, playlist: Playlist, tracks: list[Track], progress: Progress | None = None) -> list[Track]:
        added = []
        for n, t in enumerate(tracks):
            if not t.id:
                continue
            try:
                self._add_one(playlist, t)
                added.append(t)
            except (BrowserError, WebDriverException):
                try:
                    self.browser.driver.find_element("tag name", "body").send_keys(Keys.ESCAPE)
                except WebDriverException:
                    pass
            if progress:
                progress(n + 1, len(tracks))
        return added

    def _add_one(self, playlist: Playlist, track: Track) -> None:
        b = self.browser
        a = self.sel["add"]
        b.get(f"{BASE}/track/{track.id}")
        b.click(b.wait_css(a["menu_button"], timeout=20, visible=True))
        b.click_text(a["menu_items"], a["add_text"], timeout=8)
        time.sleep(0.6)
        b.click_text(a["menu_items"], playlist.name, timeout=8, exact=True)
        time.sleep(0.8)
