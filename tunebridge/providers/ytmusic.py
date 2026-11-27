"""YouTube Music (music.youtube.com) driven with Selenium.

YouTube Music refuses headless browsers ("your browser is deprecated"), so this
provider always runs in a visible browser window. You can move it to another
workspace while it works.
"""

from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, quote_plus, urlparse

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys

from ..browser import BrowserError
from ..matcher import search_query, split_artists
from ..models import Playlist, Track, parse_duration
from .base import Progress, Provider, register

BASE = "https://music.youtube.com"

# arguments[0] = row selector. Works for playlist rows and search result rows.
_ROWS_JS = r"""
const durRe = /^\d{1,2}:\d{2}(:\d{2})?$/;
const out = [];
let i = 0;
for (const row of document.querySelectorAll(arguments[0])) {
  i++;
  const titleEl = row.querySelector('.title-column .title, .title-column yt-formatted-string');
  const title = titleEl ? titleEl.innerText.trim() : '';
  const a = row.querySelector('a[href*="watch?v="]');
  const href = a ? a.getAttribute('href') : null;
  const cols = [...row.querySelectorAll('.secondary-flex-columns yt-formatted-string')]
      .map(e => ({text: e.innerText.trim(), artists: [...e.querySelectorAll('a[href*="channel/"], a[href*="browse/UC"]')].map(x => x.innerText.trim())}));
  let duration = null;
  for (const e of row.querySelectorAll('.fixed-columns yt-formatted-string, .fixed-column, .secondary-flex-columns yt-formatted-string')) {
    const parts = e.innerText.split('•').map(s => s.trim());
    for (const p of parts) if (durRe.test(p)) duration = p;
  }
  const video = href ? (new URLSearchParams(href.split('?')[1] || '')).get('v') : null;
  out.push({k: video || (title + '#' + i), pos: i, title, video, cols, duration});
}
return out;
"""

_SCROLL_JS = r"""
const rows = document.querySelectorAll(arguments[0]);
if (rows.length) rows[rows.length - 1].scrollIntoView({block: 'end'});
window.scrollBy(0, 2000);
"""

_LIBRARY_JS = r"""
const out = [];
for (const it of document.querySelectorAll(arguments[0])) {
  const a = it.querySelector('a[href*="playlist?list="], a[href*="browse/VL"]');
  if (!a) continue;
  const href = a.getAttribute('href');
  let id = null;
  const m1 = href.match(/[?&]list=([^&]+)/); const m2 = href.match(/browse\/VL([^?&/]+)/);
  id = m1 ? m1[1] : (m2 ? m2[1] : null);
  if (!id) continue;
  const t = it.querySelector('.title, .title-column .title');
  const s = it.querySelector('.subtitle, .secondary-flex-columns');
  out.push({k: id, id, name: (t ? t.innerText : a.innerText).trim(), sub: s ? s.innerText.trim() : ''});
}
return out;
"""

# Polymer elements expose their data model as `.data`; endpoints there do not depend on the UI language.
_DATA = "const tbData = (e) => e.data || (e.__data || {}).data || null;"

_CLICK_SONGS_CHIP_JS = _DATA + r"""
for (const c of document.querySelectorAll(arguments[0])) {
  const d = tbData(c); const ep = d && d.navigationEndpoint && d.navigationEndpoint.searchEndpoint;
  if (ep && (ep.params || '').startsWith(arguments[1])) { c.click(); return true; }
}
return false;
"""

_FIND_ENDPOINT_JS = _DATA + r"""
return [...document.querySelectorAll(arguments[0])].find(e => {
  const d = tbData(e) || {}; const ep = d.navigationEndpoint || d.serviceEndpoint || {};
  return !!ep[arguments[1]] && e.offsetParent !== null;
}) || null;
"""

_NOISE = {"song", "video", "single", "ep", "album", "episode", "podcast", "şarkı", "albüm", "bölüm"}
_PLAYS = re.compile(r"(plays|views|watching|dinlenme|görüntüleme|izlenme)$", re.I)
_COUNT = re.compile(r"([\d.,]+)\s+(tracks?|songs?|şarkı|parça|canciones|titres|brani|titel|músicas|песен|треков)", re.I)


def _norm(text: str) -> str:
    text = re.sub(r"\s*\n\s*", " ", text or "")
    return re.sub(r"\s+", " ", text).replace(" ,", ",").strip()


def _row_to_track(r: dict) -> Track | None:
    title = (r.get("title") or "").strip()
    if not title:
        return None
    links: list[str] = []
    tokens: list[str] = []
    for col in r.get("cols") or []:
        links += [_norm(a) for a in col.get("artists") or [] if a and _norm(a) not in links]
        tokens += [_norm(t) for t in (col.get("text") or "").split("•")]
    tokens = [t for t in tokens if t and t.lower() not in _NOISE and not _PLAYS.search(t) and parse_duration(t) is None]
    artist_tok = next((t for t in tokens if any(name in t for name in links)), tokens[0] if tokens else "")
    # linked names are kept whole ("Earth, Wind & Fire"); unlinked ones are split from the text
    rest = artist_tok
    for name in links:
        rest = rest.replace(name, ",")
    artists = links + [a for a in split_artists(rest) if a and a not in links]
    others = [t for t in tokens if t != artist_tok]
    vid = r.get("video")
    return Track(
        title=title,
        artists=artists,
        album=others[0] if others else None,
        duration=parse_duration(r.get("duration")),
        id=vid,
        url=f"{BASE}/watch?v={vid}" if vid else None,
    )


@register
class YouTubeMusicProvider(Provider):
    name = "ytmusic"
    label = "YouTube Music"
    home_url = BASE + "/"
    login_url = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fmusic.youtube.com%2F"
    headless_ok = False
    note = "Runs in a visible browser window: YouTube Music blocks headless browsers."

    def is_logged_in(self) -> bool:
        b = self.browser
        b.get(self.home_url)
        s = self.sel["session"]
        try:
            b.wait_css(s["nav"], timeout=25)
            which, _ = b.wait_any([s["logged_in"], s["logged_out"]], timeout=20)
        except BrowserError:
            return False
        return which == s["logged_in"]

    def list_playlists(self) -> list[Playlist]:
        b = self.browser
        b.get(f"{BASE}/library/playlists")
        sel = self.sel["library"]["items"]
        try:
            b.wait(lambda d: b.js(_LIBRARY_JS, sel), timeout=25, message="Library did not load.")
        except BrowserError:
            return []
        rows = self.collect_scrolling(_LIBRARY_JS, "window.scrollBy(0, 3000);", args=(sel,), pause=1.0, max_idle=2)
        out = []
        for r in rows:
            pid, sub = r["id"], r.get("sub") or ""
            m = _COUNT.search(sub)
            liked = pid == "LM"
            auto = "auto playlist" in sub.lower() or pid in ("SE",)
            out.append(Playlist(
                id=pid, name=r["name"] or pid, url=f"{BASE}/playlist?list={pid}",
                track_count=int(re.sub(r"[.,]", "", m.group(1))) if m else None,
                kind="liked" if liked else "playlist", editable=not (liked or auto),
            ))
        return out

    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]:
        b = self.browser
        b.get(f"{BASE}/playlist?list={playlist.id}")
        t = self.sel["tracks"]
        try:
            b.wait_any([t["rows"], t["rows_fallback"]], timeout=25)
        except BrowserError:
            if b.find(t["header"]):
                return []
            raise
        time.sleep(1.0)
        row_sel = t["rows"] if b.find(t["rows"]) else t["rows_fallback"]
        expected = None
        header = b.find(t["header"])
        if header:
            m = _COUNT.search(header[0].text or "")
            if m:
                expected = int(re.sub(r"[.,]", "", m.group(1)))
        rows = self.collect_scrolling(_ROWS_JS, _SCROLL_JS, args=(row_sel,), expected=expected,
                                      progress=progress, pause=1.0, max_idle=4)
        rows.sort(key=lambda r: r.get("pos") or 0)
        return [tr for tr in (_row_to_track(r) for r in rows) if tr]

    def search(self, track: Track, limit: int = 6) -> list[Track]:
        b = self.browser
        s = self.sel["search"]
        b.get(f"{BASE}/search?q={quote_plus(search_query(track))}")
        try:
            b.wait(lambda d: b.find(s["chips"]), timeout=20, message="Search did not load.")
        except BrowserError:
            return []
        before = b.driver.current_url
        if not b.js(_CLICK_SONGS_CHIP_JS, s["chips"], s["songs_params_prefix"]):
            chip = next((c for c in b.find(s["chips"]) if c.text.strip() in s["songs_chip_texts"]), None)
            if chip is None:
                return []
            b.click(chip)
        try:
            b.wait(lambda d: d.current_url != before, timeout=10)
        except BrowserError:
            pass
        time.sleep(1.5)
        rows = b.js(_ROWS_JS, s["rows"]) or []
        return [tr for tr in (_row_to_track(r) for r in rows[:limit]) if tr and tr.id]

    # ---- writing -------------------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        b = self.browser
        c = self.sel["create"]
        b.get(f"{BASE}/library/playlists")
        item = b.wait(lambda d: b.js(_FIND_ENDPOINT_JS, c["new_playlist_items"], "createPlaylistEndpoint"),
                      timeout=25, message="Could not find the 'New playlist' item.")
        b.click(item)
        b.wait_css(c["form"], timeout=10)
        inp = b.wait_css(c["title_input"], timeout=10, visible=True)
        inp.send_keys(name)
        create = b.js("const bs = [...document.querySelectorAll(arguments[0])].filter(x => x.offsetParent !== null);"
                      " return bs.length ? bs[bs.length - 1] : null;", c["create_buttons"])
        if create is None:
            raise BrowserError("Could not find the Create button.")
        before = b.driver.current_url
        b.click(create)
        try:
            b.wait(lambda d: "list=" in d.current_url and d.current_url != before, timeout=15)
            pid = parse_qs(urlparse(b.driver.current_url).query)["list"][0]
            return Playlist(id=pid, name=name, url=f"{BASE}/playlist?list={pid}", track_count=0)
        except BrowserError:
            time.sleep(2.0)
            found = self.find_playlist(name)
            if not found:
                raise BrowserError("Created the playlist but could not find it in the library.") from None
            return found

    def add_tracks(self, playlist: Playlist, tracks: list[Track], progress: Progress | None = None) -> list[Track]:
        added: list[Track] = []
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
        b.get(f"{BASE}/watch?v={track.id}")
        b.click(b.wait_css(a["menu_button"], timeout=25, visible=True))
        save = b.wait(lambda d: b.js(_FIND_ENDPOINT_JS, a["menu_items"], "addToPlaylistEndpoint"),
                      timeout=10, message="No 'Save to playlist' item in the menu.")
        b.click(save)
        b.wait_css(a["dialog"], timeout=10)
        time.sleep(0.6)
        want = playlist.name.strip().lower()

        def option(d):
            for el in b.find(a["dialog_options"]):
                first = (el.text or "").split("\n")[0].strip().lower()
                if first == want and el.is_displayed():
                    return el
            return False

        b.click(b.wait(option, timeout=10, message=f"Playlist '{playlist.name}' is not in the save dialog."))
        time.sleep(1.0)
        # "already in this playlist" dialog: take the cancel/skip side so nothing is duplicated
        for el in b.find(a["duplicate_cancel"]):
            if el.is_displayed():
                b.click(el)
                break
