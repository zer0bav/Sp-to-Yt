"""Track matching: decide which search result on the target platform is the same song."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from .models import Track

MATCHED = 80.0  # at or above: added automatically
REVIEW = 60.0  # between REVIEW and MATCHED: shown for confirmation

# Words that change what a recording is. If the candidate has one and the source does not, it is
# probably a different version.
_VERSION_WORDS = (
    "live", "remix", "karaoke", "instrumental", "cover", "acoustic", "sped up", "slowed",
    "nightcore", "8d", "reverb", "8 bit", "lyrics", "lyric video", "tribute", "demo", "a cappella",
)

_FEAT = re.compile(r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.I)
_FEAT_TAIL = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s.+$", re.I)
_NOISE_BRACKET = re.compile(
    r"\s*[\(\[][^\)\]]*(remaster|remastered|version|edit|mono|stereo|deluxe|bonus|explicit|clean|"
    r"official|audio|video|visuali[sz]er|from |original|soundtrack|anniversary|mix\b)[^\)\]]*[\)\]]",
    re.I,
)
_DASH_SUFFIX = re.compile(
    r"\s+-\s+.*(remaster|remastered|version|edit|mono|stereo|single|radio|bonus|from |\d{4}).*$", re.I
)
_PUNCT = re.compile(r"[^\w\s]", re.U)
_SPACES = re.compile(r"\s+")
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|/|\band\b|(?<=\s)x(?=\s)|\bvs\.?(?=\s)|\bfeat\.?(?=\s)|\bft\.?(?=\s)|\bfeaturing\b)\s*", re.I
)

_TRANSLIT = str.maketrans({"ı": "i", "ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ł": "l"})


def fold(text: str) -> str:
    """Lowercase, strip accents (ş->s, é->e), drop punctuation, squeeze spaces."""
    text = unicodedata.normalize("NFKD", text.lower().translate(_TRANSLIT))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def clean_title(title: str) -> str:
    """Remove featuring credits and edition noise, keep the real song name."""
    t = _FEAT.sub("", title)
    t = _NOISE_BRACKET.sub("", t)
    t = _DASH_SUFFIX.sub("", t)
    t = _FEAT_TAIL.sub("", t)
    return t.strip() or title.strip()


def split_artists(text: str) -> list[str]:
    return [a.strip() for a in _ARTIST_SPLIT.split(text) if a and a.strip()]


def search_query(track: Track) -> str:
    artist = track.artists[0] if track.artists else ""
    return f"{clean_title(track.title)} {artist}".strip()


def _version_penalty(src_title: str, cand_title: str) -> float:
    s, c = fold(src_title), fold(cand_title)
    penalty = 0.0
    for w in _VERSION_WORDS:
        pat = rf"\b{re.escape(w)}\b"
        if re.search(pat, c) and not re.search(pat, s):
            penalty += 18.0
    return min(penalty, 36.0)


def _artist_score(src: Track, cand: Track) -> float:
    if not src.artists or not cand.artists:
        return 60.0  # unknown: neither reward nor punish much
    src_names = [fold(a) for a in src.artists for a in split_artists(a)]
    cand_names = [fold(a) for a in cand.artists for a in split_artists(a)]
    best = 0.0
    for s in src_names:
        for c in cand_names:
            best = max(best, fuzz.ratio(s, c), fuzz.token_set_ratio(s, c) - 5)
    joined = fuzz.token_set_ratio(" ".join(src_names), " ".join(cand_names))
    return max(best, joined)


def _duration_score(src: Track, cand: Track) -> float | None:
    if not src.duration or not cand.duration:
        return None
    diff = abs(src.duration - cand.duration)
    if diff <= 3:
        return 100.0
    if diff <= 8:
        return 85.0
    if diff <= 15:
        return 60.0
    if diff <= 30:
        return 30.0
    return 0.0


def score(src: Track, cand: Track) -> float:
    """0-100 confidence that `cand` is the same recording as `src`."""
    if src.isrc and cand.isrc and src.isrc.upper() == cand.isrc.upper():
        return 100.0
    st, ct = fold(clean_title(src.title)), fold(clean_title(cand.title))
    title = max(fuzz.ratio(st, ct), fuzz.token_sort_ratio(st, ct))
    # "Song" vs "Song (Official Video)" style titles on YouTube still contain the full name
    if st and st in ct:
        title = max(title, 92.0)
    artist = _artist_score(src, cand)
    # YouTube videos often put the artist in the title: "Artist - Song"
    if artist < 70 and src.artists and fold(src.artists[0]) in fold(cand.title):
        artist = 85.0
    dur = _duration_score(src, cand)
    if dur is None:
        total = title * 0.6 + artist * 0.4
    else:
        total = title * 0.5 + artist * 0.32 + dur * 0.18
    total -= _version_penalty(src.title, cand.title)
    return round(max(0.0, min(100.0, total)), 1)


@dataclass
class Match:
    track: Track | None
    score: float
    candidates: list[tuple[Track, float]]

    @property
    def status(self) -> str:
        if self.track is None or self.score < REVIEW:
            return "not_found"
        return "matched" if self.score >= MATCHED else "review"


def best_match(src: Track, candidates: list[Track]) -> Match:
    scored = sorted(((c, score(src, c)) for c in candidates), key=lambda x: x[1], reverse=True)
    if not scored:
        return Match(None, 0.0, [])
    top, s = scored[0]
    return Match(top, s, scored[:5])
