"""Apple Music web player (music.apple.com) driven with Selenium.

Needs an Apple Music subscription for your library; no developer account. Reading
public playlists and searching work on the live site. Library access and writing
are experimental until checked against a real account.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs, quote, urlparse

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys

from ..browser import BrowserError
from ..matcher import search_query
from ..models import Playlist, Track, parse_duration
from .base import Progress, Provider, register

BASE = "https://music.apple.com"

_ROWS_JS = r"""
const [rowSel, titleSel, linkSel, bySel, albumSel, durSel] = arguments;
const out = [];
for (const row of document.querySelectorAll(rowSel)) {
  const t = row.querySelector(titleSel);
  if (!t) continue;
  const link = row.querySelector(linkSel);
  const al = row.querySelector(albumSel);
  const d = row.querySelector(durSel);
  out.push({k: row.getAttribute('data-row') || (link && link.href) || t.innerText,
            pos: parseInt(row.getAttribute('data-row') || '0'),
            title: t.innerText.trim(), href: link ? link.href : null,
            artists: [...row.querySelectorAll(bySel)].map(a => a.innerText.trim()).filter(Boolean),
            album: al ? al.innerText.trim() : null, duration: d ? d.innerText.trim() : null});
}
return out;
"""

_SEARCH_JS = r"""
const [rowSel, titleSel, artistSel] = arguments;
return [...document.querySelectorAll(rowSel)].map((row, i) => {
  const t = row.querySelector(titleSel);
  return {k: String(i), title: t ? t.innerText.trim() : '', href: t ? t.href : null,
          artists: [...row.querySelectorAll(artistSel)].map(a => a.innerText.trim()).filter(Boolean)};
});
"""

_SCROLL_JS = r"""
const rows = document.querySelectorAll(arguments[0]);
if (rows.length) rows[rows.length - 1].scrollIntoView({block: 'end'});
"""


def _song_id(href: str | None) -> str | None:
    if not href:
        return None
    q = parse_qs(urlparse(href).query)
    if "i" in q:
        return q["i"][0]
    m = re.search(r"/song/[^/]+/(\d+)", href)
    return m.group(1) if m else None


@register
class AppleMusicProvider(Provider):
    name = "applemusic"
    label = "Apple Music"
    home_url = BASE + "/"
    login_url = BASE + "/login"
    headless_ok = True
    experimental = True
    note = "Needs an Apple Music subscription. Reading and search verified; writing is experimental."

    @property
    def storefront(self) -> str:
        env = os.environ.get("TUNEBRIDGE_APPLE_STOREFRONT")
        if env:
            return env
        try:
            sf = self.browser.js("try { return MusicKit.getInstance().storefrontId } catch (e) { return null }")
        except WebDriverException:
            sf = None
        return sf or "us"

    def is_logged_in(self) -> bool:
        b = self.browser
        b.get(self.home_url)
        s = self.sel["session"]
        try:
            which, _ = b.wait_any([s["logged_in"], s["logged_out"]], timeout=25)
        except BrowserError:
            return False
        return which == s["logged_in"]

    def _track_rows(self, expected: int | None, progress: Progress | None) -> list[Track]:
        t = self.sel["tracks"]
        args = (t["row"], t["title"], t["song_link"], t["by_line"], t["album"], t["duration"])
        rows = self.collect_scrolling(_ROWS_JS, _SCROLL_JS, args=args, expected=expected, progress=progress)
        rows.sort(key=lambda r: r.get("pos") or 0)
        out = []
        for r in rows:
            sid = _song_id(r.get("href"))
            out.append(Track(title=r["title"], artists=r.get("artists") or [], album=r.get("album"),
                             duration=parse_duration(r.get("duration")), id=sid, url=r.get("href")))
        return out

    def list_playlists(self) -> list[Playlist]:
        b = self.browser
        lib = self.sel["library"]
        b.get(lib["url"])
        try:
            b.wait_css(lib["items"], timeout=25)
        except BrowserError:
            return []
        time.sleep(1.5)
        items = b.js(
            "return [...document.querySelectorAll(arguments[0])].map(a => ({href: a.href, name: (a.getAttribute('aria-label') || a.innerText || '').trim()}))",
            lib["items"],
        ) or []
        out: dict[str, Playlist] = {}
        for it in items:
            m = re.search(r"/library/playlist/([^/?#]+)", it.get("href") or "")
            if m and it.get("name") and m.group(1) not in out:
                out[m.group(1)] = Playlist(id=m.group(1), name=it["name"].split("\n")[0], url=it["href"])
        return list(out.values())

    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]:
        b = self.browser
        b.get(playlist.url or f"{BASE}/library/playlist/{playlist.id}")
        try:
            b.wait_css(self.sel["tracks"]["row"], timeout=25)
        except BrowserError:
            return []
        time.sleep(1.0)
        return self._track_rows(playlist.track_count, progress)

    def search(self, track: Track, limit: int = 6) -> list[Track]:
        b = self.browser
        s = self.sel["search"]
        b.get(f"{BASE}/{self.storefront}/search?term={quote(search_query(track))}")
        try:
            b.wait_css(s["row"], timeout=15)
        except BrowserError:
            return []
        time.sleep(0.6)
        rows = b.js(_SEARCH_JS, s["row"], s["title"], s["artists"]) or []
        out = []
        for r in rows[:limit]:
            sid = _song_id(r.get("href"))
            if r.get("title") and sid:
                out.append(Track(title=r["title"], artists=r.get("artists") or [], id=sid, url=r.get("href")))
        return out

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        b = self.browser
        c = self.sel["create"]
        b.get(self.sel["library"]["url"])
        time.sleep(2.0)
        b.click_text(c["buttons"], c["open_texts"][0], timeout=15, exact=True)
        inp = b.wait_css(c["title_input"], timeout=10, visible=True)
        inp.send_keys(Keys.CONTROL, "a")
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
            raise BrowserError("Created the playlist but could not find it in your library.")
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
        b.get(f"{BASE}/{self.storefront}/song/{track.id}")
        b.click(b.wait_css(a["more_button"], timeout=20, visible=True))
        b.click_text(a["menu_items"], a["add_text"], timeout=8)
        time.sleep(0.6)
        b.click_text(a["menu_items"], playlist.name, timeout=8, exact=True)
        time.sleep(0.8)
