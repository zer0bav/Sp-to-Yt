"""Deezer (www.deezer.com) driven with Selenium.

Reading playlists and searching work on the live site. Creating playlists and
adding songs follow the site's menus and are marked experimental until they have
been checked against a real account.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys

from ..browser import BrowserError
from ..matcher import fold, search_query
from ..models import Playlist, Track, parse_duration
from .base import Progress, Provider, register

BASE = "https://www.deezer.com"

_ROWS_JS = r"""
const [rowSel, titleSel, artistSel, albumSel, durSel] = arguments;
const out = [];
for (const row of document.querySelectorAll(rowSel)) {
  const t = row.querySelector(titleSel);
  if (!t) continue;
  const idx = parseInt(row.getAttribute('aria-rowindex') || '0');
  const artists = [...row.querySelectorAll(artistSel)].map(a => a.innerText.trim()).filter(Boolean);
  const al = row.querySelector(albumSel);
  const d = row.querySelector(durSel);
  const link = row.querySelector('a[href*="/track/"]');
  out.push({k: String(idx), idx, title: t.innerText.trim(), artists, album: al ? al.innerText.trim() : null,
            duration: d ? d.innerText.trim() : null, href: link ? link.getAttribute('href') : null});
}
return out;
"""

_SCROLL_JS = r"""
const rows = document.querySelectorAll(arguments[0]);
if (rows.length) rows[rows.length - 1].scrollIntoView({block: 'end'});
"""

_NUM_PREFIX = re.compile(r"^\d+\.\s+")


def _to_track(r: dict) -> Track | None:
    title = _NUM_PREFIX.sub("", (r.get("title") or "").strip())
    if not title:
        return None
    href = r.get("href") or ""
    tid = href.rsplit("/", 1)[-1] if "/track/" in href else None
    return Track(title=title, artists=list(r.get("artists") or []), album=r.get("album"),
                 duration=parse_duration(r.get("duration")), id=tid,
                 url=f"{BASE}/track/{tid}" if tid else None)


@register
class DeezerProvider(Provider):
    name = "deezer"
    label = "Deezer"
    home_url = BASE + "/en/"
    login_url = BASE + "/login"
    headless_ok = True
    experimental = True
    note = "Reading and search verified; writing is experimental."

    def is_logged_in(self) -> bool:
        b = self.browser
        b.get(self.home_url)
        s = self.sel["session"]
        try:
            which, _ = b.wait_any([s["logged_in"], s["logged_out"]], timeout=25)
        except BrowserError:
            return False
        return which == s["logged_in"]

    def _rows(self, expected: int | None = None, progress: Progress | None = None, scroll: bool = True) -> list[dict]:
        t = self.sel["tracks"]
        args = (t["row"], t["title"], t["artist"], t["album"], t["duration"])
        if not scroll:
            return self.browser.js(_ROWS_JS, *args) or []
        rows = self.collect_scrolling(_ROWS_JS, _SCROLL_JS, args=args, expected=expected, progress=progress)
        return sorted(rows, key=lambda r: r.get("idx") or 0)

    def list_playlists(self) -> list[Playlist]:
        b = self.browser
        b.get(self.home_url)
        s = self.sel["session"]
        link = b.wait_css(s["profile_link"], timeout=25)
        m = re.search(r"/profile/(\d+)", link.get_attribute("href") or "")
        if not m:
            raise BrowserError("Could not find your Deezer profile.")
        uid = m.group(1)
        b.get(f"{BASE}/en/profile/{uid}/playlists")
        time.sleep(3.0)
        items = b.js(
            """return [...document.querySelectorAll(arguments[0])].map(a => ({href: a.getAttribute('href'),
                 name: (a.getAttribute('aria-label') || a.innerText || '').trim()}))""",
            self.sel["library"]["items"],
        ) or []
        out: dict[str, Playlist] = {}
        for it in items:
            pm = re.search(r"/playlist/(\d+)", it.get("href") or "")
            if pm and it.get("name") and pm.group(1) not in out:
                out[pm.group(1)] = Playlist(id=pm.group(1), name=it["name"].split("\n")[0],
                                            url=f"{BASE}/en/playlist/{pm.group(1)}")
        out_list = list(out.values())
        out_list.insert(0, Playlist(id="loved", name="Favourite tracks", kind="liked", editable=False,
                                    url=f"{BASE}/en/profile/{uid}/loved"))
        return out_list

    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]:
        b = self.browser
        b.get(playlist.url or f"{BASE}/en/playlist/{playlist.id}")
        try:
            b.wait_css(self.sel["tracks"]["title"], timeout=25)
        except BrowserError:
            return []
        time.sleep(1.0)
        expected = b.js("const g=document.querySelector('[role=\"grid\"][aria-rowcount]'); return g ? parseInt(g.getAttribute('aria-rowcount')) - 1 : null;")
        return [t for t in (_to_track(r) for r in self._rows(expected if expected and expected > 0 else None, progress)) if t]

    def search(self, track: Track, limit: int = 6) -> list[Track]:
        b = self.browser
        q = search_query(track)
        b.get(f"{BASE}/search/{quote(q, safe='')}/track")
        try:
            b.wait_css(self.sel["tracks"]["title"], timeout=15)
        except BrowserError:
            return []
        time.sleep(0.8)
        rows = sorted(self._rows(scroll=False), key=lambda r: r.get("idx") or 0)[:limit]
        out = []
        for r in rows:
            t = _to_track(r)
            if t:
                # Deezer rows carry no track id; remember how to find the row again when adding
                t.id = t.id or f"q:{q}|{t.title}|{t.artist_line}"
                out.append(t)
        return out

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        b = self.browser
        c = self.sel["create"]
        before = {p.id for p in self.list_playlists()}
        for text in c["open_texts"]:
            try:
                b.click_text(c["buttons"], text, timeout=6)
                break
            except BrowserError:
                continue
        else:
            raise BrowserError("Could not find Deezer's 'Create playlist' button.")
        inp = b.wait_css(c["title_input"], timeout=10, visible=True)
        inp.send_keys(name)
        for text in c["confirm_texts"]:
            try:
                b.click_text('[role="dialog"] button', text, timeout=4, exact=True)
                break
            except BrowserError:
                continue
        time.sleep(2.5)
        for p in self.list_playlists():
            if p.id not in before and p.name.strip().lower() == name.strip().lower():
                return p
        found = self.find_playlist(name)
        if not found:
            raise BrowserError("Created the playlist but could not find it.")
        return found

    def add_tracks(self, playlist: Playlist, tracks: list[Track], progress: Progress | None = None) -> list[Track]:
        added = []
        for n, t in enumerate(tracks):
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
        q = track.id[2:].split("|", 1)[0] if track.id and track.id.startswith("q:") else search_query(track)
        b.get(f"{BASE}/search/{quote(q, safe='')}/track")
        b.wait_css(self.sel["tracks"]["title"], timeout=15)
        time.sleep(0.8)
        want_t, want_a = fold(track.title), fold(track.artists[0]) if track.artists else ""
        row_el = None
        for row in b.find(self.sel["tracks"]["row"]):
            t_el = row.find_elements("css selector", self.sel["tracks"]["title"])
            a_el = row.find_elements("css selector", self.sel["tracks"]["artist"])
            if t_el and fold(_NUM_PREFIX.sub("", t_el[0].text)) == want_t and (
                not want_a or (a_el and fold(a_el[0].text) == want_a)
            ):
                row_el = row
                break
        if row_el is None:
            raise BrowserError("Song not found again on Deezer.")
        b.js("arguments[0].scrollIntoView({block:'center'});", row_el)
        menu = row_el.find_elements("css selector", a["menu_button"])
        if not menu:
            raise BrowserError("No menu button on the Deezer row.")
        b.click(menu[0])
        b.click_text(a["menu_items"], a["add_text"], timeout=8)
        time.sleep(0.6)
        b.click_text(a["menu_items"], playlist.name, timeout=8, exact=True)
        time.sleep(0.8)
