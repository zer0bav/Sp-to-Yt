"""Spotify through the web player (open.spotify.com), driven with Selenium."""

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

BASE = "https://open.spotify.com"

# Picks the track grid to read: `scopeSel` when given (the playlist's own list, not the
# "Recommended" list under it), otherwise the first grid that has track rows.
_SCOPE_JS = r"""
function tbScope(rowSel, scopeSel) {
  if (scopeSel) return document.querySelector(scopeSel);
  return [...document.querySelectorAll('[role="grid"]')].find(g => g.querySelector(rowSel)) || document;
}
"""

# Reads every rendered track row. arguments = row selector, title link selector, scope selector
_ROWS_JS = _SCOPE_JS + r"""
const [rowSel, linkSel, scopeSel] = arguments;
const scope = tbScope(rowSel, scopeSel);
if (!scope) return [];
const out = [];
const durRe = /^\d{1,2}:\d{2}(:\d{2})?$/;
for (const row of scope.querySelectorAll(rowSel)) {
  const idx = row.parentElement && row.parentElement.getAttribute('aria-rowindex');
  const link = row.querySelector(linkSel);
  let title = link ? link.innerText.trim() : '';
  if (!title) {
    const t = row.querySelector('[aria-colindex="2"] [dir="auto"]');
    title = t ? t.innerText.trim() : '';
  }
  const href = link ? link.getAttribute('href') : null;
  const artists = [...new Set([...row.querySelectorAll('a[href^="/artist/"]')]
      .map(a => a.innerText.trim()).filter(Boolean))];
  const albumEl = row.querySelector('a[href^="/album/"]');
  let duration = null;
  for (const el of row.querySelectorAll('div, span')) {
    if (el.childElementCount === 0 && durRe.test(el.textContent.trim())) duration = el.textContent.trim();
  }
  out.push({k: idx || href || title, idx: idx ? parseInt(idx) : null, title, href, artists,
            album: albumEl ? albumEl.innerText.trim() : null, duration});
}
return out;
"""

_SCROLL_ROWS_JS = _SCOPE_JS + r"""
const scope = tbScope(arguments[0], arguments[2]);
const rows = scope ? scope.querySelectorAll(arguments[0]) : [];
if (rows.length) {
  const last = rows[rows.length - 1];
  let e = last.parentElement;
  while (e && !(e.scrollHeight > e.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(e).overflowY))) e = e.parentElement;
  const vr = e ? e.getBoundingClientRect() : {top: 0, bottom: innerHeight};
  // rows still loading (placeholders) on screen: wait instead of scrolling past them
  const pending = [...scope.querySelectorAll('[role="row"][aria-rowindex]')].some(r => {
    if (r.getAttribute('aria-rowindex') === '1' || r.querySelector(arguments[0])) return false;
    const b = r.getBoundingClientRect();
    return b.height > 0 && b.bottom > vr.top && b.top < vr.bottom;
  });
  if (!pending) {
    if (e) e.scrollTop += Math.max(300, e.clientHeight * 0.6); else last.scrollIntoView({block: 'end'});
  }
}
"""

_ROWCOUNT_JS = _SCOPE_JS + r"""
const g = tbScope(arguments[1], arguments[2]);
return g && g.getAttribute && g.getAttribute('aria-rowcount') ? parseInt(g.getAttribute('aria-rowcount')) : null;
"""

_LIBRARY_JS = r"""
const [titlePrefix, subPrefix, fallback] = arguments;
const out = []; const seen = new Set();
for (const el of document.querySelectorAll('[id^="' + titlePrefix + 'spotify:"]')) {
  const uri = el.id.slice(titlePrefix.length);
  if (seen.has(uri)) continue; seen.add(uri);
  const sub = document.getElementById(subPrefix + uri);
  out.push({k: uri, uri, name: el.innerText.trim(), sub: sub ? sub.innerText.trim() : ''});
}
for (const a of document.querySelectorAll(fallback)) {
  const id = (a.getAttribute('href') || '').split('/')[2];
  const uri = 'spotify:playlist:' + id;
  if (!id || seen.has(uri)) continue; seen.add(uri);
  out.push({k: uri, uri, name: (a.innerText || a.getAttribute('aria-label') || '').trim(), sub: ''});
}
return out;
"""

_SCROLL_LIBRARY_JS = r"""
const els = document.querySelectorAll('[id^="' + arguments[0] + 'spotify:"]');
if (!els.length) return;
let e = els[els.length - 1];
while (e && !(e.scrollHeight > e.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(e).overflowY))) e = e.parentElement;
if (!e) { els[els.length - 1].scrollIntoView({block: 'end'}); return; }
const vr = e.getBoundingClientRect();
// placeholder rows still loading on screen: wait for them instead of scrolling past
const pending = [...e.querySelectorAll('[role="row"]')].some(r => {
  if (r.querySelector('[id^="' + arguments[0] + '"]')) return false;
  const b = r.getBoundingClientRect();
  return b.height > 20 && b.bottom > vr.top && b.top < vr.bottom;
});
if (!pending) e.scrollTop += Math.max(200, e.clientHeight * 0.6);
"""

_LIBRARY_ROWCOUNT_JS = r"""
const el = document.querySelector('[id^="' + arguments[0] + 'spotify:"]');
const g = el && el.closest('[role="grid"][aria-rowcount]');
return g ? parseInt(g.getAttribute('aria-rowcount')) : null;
"""


_SCROLL_LIBRARY_TOP_JS = r"""
let e = document.querySelector('[id^="' + arguments[0] + 'spotify:"]');
while (e && !(e.scrollHeight > e.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(e).overflowY))) e = e.parentElement;
if (e) e.scrollTop = 0;
"""


def _row_to_track(r: dict) -> Track | None:
    title = (r.get("title") or "").strip()
    if not title:
        return None
    href = r.get("href") or ""
    tid = href.rsplit("/", 1)[-1] if "/track/" in href else None
    return Track(
        title=title,
        artists=list(r.get("artists") or []),
        album=r.get("album"),
        duration=parse_duration(r.get("duration")),
        id=tid,
        url=f"{BASE}/track/{tid}" if tid else None,
    )


@register
class SpotifyProvider(Provider):
    name = "spotify"
    label = "Spotify"
    home_url = BASE + "/"
    login_url = "https://accounts.spotify.com/login?continue=https%3A%2F%2Fopen.spotify.com%2F"
    headless_ok = True
    note = "Free and Premium accounts both work."

    _user: str | None = None

    # ---- session -------------------------------------------------------------------

    def is_logged_in(self) -> bool:
        b = self.browser
        b.get(self.home_url)
        s = self.sel["session"]
        try:
            which, el = b.wait_any([s["logged_in"], s["logged_out"]], timeout=25)
        except BrowserError:
            return False
        if which == s["logged_in"]:
            self._user = (el.get_attribute("aria-label") or "").strip() or None
            return True
        return False

    # ---- reading -------------------------------------------------------------------

    def _library_rows(self) -> list[dict]:
        """Rows currently shown in the Your Library sidebar (top level or inside an open folder)."""
        b = self.browser
        lib = self.sel["library"]
        args = (lib["title_id_prefix"], lib["subtitle_id_prefix"], lib["fallback_links"])
        try:
            b.wait(lambda d: b.js(_LIBRARY_JS, *args), timeout=10)
        except BrowserError:
            # the sidebar may have been left collapsed; open it and try once more
            for sel in lib["expand_buttons"]:
                els = [e for e in b.find(sel) if e.is_displayed()]
                if els:
                    b.click(els[0])
                    break
            try:
                b.wait(lambda d: b.js(_LIBRARY_JS, *args), timeout=15, message="Your Library did not load.")
            except BrowserError:
                return []
        b.js(_SCROLL_LIBRARY_TOP_JS, lib["title_id_prefix"])
        time.sleep(0.6)
        expected = b.js(_LIBRARY_ROWCOUNT_JS, lib["title_id_prefix"])
        return self.collect_scrolling(_LIBRARY_JS, _SCROLL_LIBRARY_JS, args=args, pause=0.7, max_idle=4,
                                      expected=expected if isinstance(expected, int) and expected > 0 else None)

    def _to_top_level(self) -> None:
        """Leave any folder the sidebar remembers being inside."""
        b = self.browser
        for _ in range(6):
            back = [e for e in b.find(self.sel["library"]["folder_back"]) if e.is_displayed()]
            if not back:
                return
            b.click(back[0])
            time.sleep(1.0)

    def _open_folder(self, uri: str) -> bool:
        """Open a Your Library folder in the sidebar; its playlists then replace the top-level rows."""
        b = self.browser
        lib = self.sel["library"]
        prefix = lib["title_id_prefix"]
        args = (prefix, lib["subtitle_id_prefix"], lib["fallback_links"])
        b.get(self.home_url)
        try:
            b.wait(lambda d: b.js(_LIBRARY_JS, *args), timeout=15)
        except BrowserError:
            return False
        self._to_top_level()
        b.js(_SCROLL_LIBRARY_TOP_JS, prefix)
        time.sleep(0.5)
        for _ in range(40):
            el = b.js("return document.getElementById(arguments[0])", prefix + uri)
            if el is not None:
                row = b.js("let e=arguments[0]; while (e && e.getAttribute('role') !== 'row') e = e.parentElement;"
                           " return e || arguments[0];", el)
                b.click(row)
                time.sleep(1.5)
                return True
            b.js(_SCROLL_LIBRARY_JS, prefix)
            time.sleep(0.4)
        return False

    def list_playlists(self) -> list[Playlist]:
        b = self.browser
        b.get(self.home_url)
        lib = self.sel["library"]
        try:
            b.wait(lambda d: b.js(_LIBRARY_JS, lib["title_id_prefix"], lib["subtitle_id_prefix"], lib["fallback_links"]), timeout=10)
        except BrowserError:
            pass  # _library_rows below retries and expands the sidebar
        self._to_top_level()
        rows = self._library_rows()
        seen = {r["uri"] for r in rows}
        folders = [r["uri"] for r in rows if ":folder:" in r["uri"]]
        opened: set[str] = set()
        while folders:
            f = folders.pop(0)
            if f in opened:
                continue
            opened.add(f)
            if not self._open_folder(f):
                continue
            for r in self._library_rows():
                if r["uri"] not in seen:
                    seen.add(r["uri"])
                    rows.append(r)
                    if ":folder:" in r["uri"]:
                        folders.append(r["uri"])
        if opened:
            self._to_top_level()
        out: list[Playlist] = []
        for r in rows:
            uri: str = r["uri"]
            sub: str = r.get("sub") or ""
            if uri == "spotify:collection:tracks":
                out.append(Playlist(id="liked", name=r["name"] or "Liked Songs", kind="liked",
                                    url=f"{BASE}/collection/tracks", editable=False,
                                    track_count=_count(sub), owner=self._user))
                continue
            if not uri.startswith("spotify:playlist:") or not (r.get("name") or "").strip():
                continue
            pid = uri.split(":")[-1]
            owner = _owner(sub)
            out.append(Playlist(
                id=pid, name=r["name"], url=f"{BASE}/playlist/{pid}", owner=owner, track_count=_count(sub),
                editable=owner is None or self._user is None or owner == self._user,
            ))
        return out

    def get_tracks(self, playlist: Playlist, progress: Progress | None = None) -> list[Track]:
        b = self.browser
        url = playlist.url or (f"{BASE}/collection/tracks" if playlist.kind == "liked" else f"{BASE}/playlist/{playlist.id}")
        b.get(url)
        t = self.sel["tracks"]
        scope = None if playlist.kind == "liked" else t["playlist_grid"]
        try:
            b.wait_css(t["row"], timeout=25)
        except BrowserError:
            if b.find(t["empty_hint"]):
                return []
            raise
        time.sleep(1.0)
        if scope and not b.find(scope):
            return []  # empty playlist: only Spotify's recommendations are on the page
        rowcount = b.js(_ROWCOUNT_JS, t["grid"], t["row"], scope)
        expected = rowcount - 1 if isinstance(rowcount, int) and rowcount > 0 else None
        rows = self.collect_scrolling(_ROWS_JS, _SCROLL_ROWS_JS, args=(t["row"], t["title_link"], scope),
                                      expected=expected, progress=progress, pause=0.9, max_idle=5)
        rows.sort(key=lambda r: r.get("idx") or 0)
        return [tr for tr in (_row_to_track(r) for r in rows) if tr]

    def search(self, track: Track, limit: int = 6) -> list[Track]:
        b = self.browser
        q = search_query(track)
        b.get(f"{BASE}/search/{quote(q, safe='')}/tracks")
        t = self.sel["tracks"]
        try:
            b.wait_css(t["row"], timeout=15)
        except BrowserError:
            return []
        time.sleep(0.6)
        rows = b.js(_ROWS_JS, t["row"], t["title_link"], None) or []
        rows.sort(key=lambda r: r.get("idx") or 0)
        return [tr for tr in (_row_to_track(r) for r in rows[:limit]) if tr]

    # ---- writing -------------------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        b = self.browser
        c = self.sel["create"]
        b.get(self.home_url)
        btn = None
        for sel in c["buttons"]:
            try:
                btn = b.wait_css(sel, timeout=8, visible=True)
                break
            except BrowserError:
                continue
        if btn is None:
            raise BrowserError("Could not find Spotify's 'Create' button.")
        before = b.driver.current_url
        b.click(btn)
        b.click_text(c["menu_items"], c["playlist_item_text"], timeout=10)
        b.wait(lambda d: "/playlist/" in d.current_url and d.current_url != before, timeout=20,
               message="Spotify did not open the new playlist.")
        pid = b.driver.current_url.split("/playlist/")[1].split("?")[0].split("/")[0]
        self._rename(name, description)
        return Playlist(id=pid, name=name, url=f"{BASE}/playlist/{pid}", track_count=0, owner=self._user)

    def _rename(self, name: str, description: str) -> None:
        b = self.browser
        c = self.sel["create"]
        time.sleep(1.0)
        for sel in c["title_buttons"]:
            els = [e for e in b.find(sel) if e.is_displayed()]
            if els:
                b.click(els[0])
                break
        inp = b.wait_css(c["name_input"], timeout=10, visible=True)
        inp.send_keys(Keys.CONTROL, "a")
        inp.send_keys(Keys.DELETE)
        inp.send_keys(name)
        if description:
            for el in b.find(c["description_input"]):
                el.send_keys(Keys.CONTROL, "a")
                el.send_keys(description)
                break
        b.click(b.wait_css(c["save_button"], timeout=10))
        time.sleep(1.0)

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
        b.get(f"{BASE}/track/{track.id}")
        b.click(b.wait_css(a["more_button"], timeout=20, visible=True))
        b.click_text(a["menu_items"], a["add_to_playlist_text"], timeout=10)
        time.sleep(0.5)
        filt = b.find(a["playlist_filter"])
        if filt:
            filt[0].send_keys(playlist.name)
            time.sleep(0.8)
        b.click_text(a["menu_items"], playlist.name, timeout=10, exact=True)
        time.sleep(0.8)
        # "Already added" dialog: never add duplicates
        for label in a["duplicate_dialog_buttons"]:
            hits = [e for e in b.find('[role="dialog"] button') if e.text.strip().lower() == label.lower()]
            if hits:
                b.click(hits[0])
                break


_COUNT = re.compile(r"([\d.,]+)\s+(songs?|tracks?)", re.I)


def _count(sub: str) -> int | None:
    m = _COUNT.search(sub or "")
    return int(re.sub(r"[.,]", "", m.group(1))) if m else None


def _owner(sub: str) -> str | None:
    # "Playlist • Owner Name" or "Playlist • 25 songs"
    parts = [p.strip() for p in (sub or "").split("•")]
    if len(parts) >= 2 and not _COUNT.search(parts[1]):
        return parts[1] or None
    return None
