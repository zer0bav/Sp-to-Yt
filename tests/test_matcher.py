from tunebridge.matcher import MATCHED, REVIEW, best_match, clean_title, fold, score, search_query, split_artists
from tunebridge.models import Track, parse_duration


def t(title, *artists, d=None):
    return Track(title=title, artists=list(artists), duration=d)


def test_fold_strips_accents_and_turkish_letters():
    assert fold("Yalnızlık Senfonisi") == "yalnizlik senfonisi"
    assert fold("Beyoncé!") == "beyonce"
    assert fold("  AC/DC  ") == "ac dc"


def test_clean_title_removes_edition_noise():
    assert clean_title("Bohemian Rhapsody - Remastered 2011") == "Bohemian Rhapsody"
    assert clean_title("Song (feat. Someone)") == "Song"
    assert clean_title("Song [Official Video]") == "Song"
    assert clean_title("Song ft. Someone") == "Song"
    assert clean_title("Song (From \"A Movie\")") == "Song"
    # a real part of the title stays
    assert clean_title("Live Forever") == "Live Forever"


def test_split_artists():
    assert split_artists("KAROL G, Judeline & rusowsky") == ["KAROL G", "Judeline", "rusowsky"]
    assert split_artists("Daft Punk feat. Pharrell") == ["Daft Punk", "Pharrell"]


def test_same_song_scores_high():
    src = t("Bohemian Rhapsody - Remastered 2011", "Queen", d=355)
    assert score(src, t("Bohemian Rhapsody", "Queen", d=354)) >= 95


def test_live_and_remix_versions_are_penalised():
    src = t("Bohemian Rhapsody", "Queen", d=355)
    live = score(src, t("Bohemian Rhapsody (Live Aid)", "Queen", d=330))
    studio = score(src, t("Bohemian Rhapsody", "Queen", d=355))
    assert studio - live > 20
    assert score(src, t("Bohemian Rhapsody (Remix)", "DJ X", d=400)) < REVIEW


def test_wrong_artist_is_not_a_match():
    src = t("Hello", "Adele", d=295)
    assert score(src, t("Hello", "Lionel Richie", d=246)) < MATCHED


def test_youtube_style_title_with_artist():
    src = t("Nightcall", "Kavinsky", d=258)
    assert score(src, t("Kavinsky - Nightcall (Official Video)", "KavinskyVEVO", d=260)) >= REVIEW


def test_isrc_wins():
    a = Track(title="x", artists=["y"], isrc="USUM71703861")
    b = Track(title="totally different", artists=["z"], isrc="usum71703861")
    assert score(a, b) == 100


def test_best_match_orders_candidates():
    src = t("Midnight City", "M83", d=244)
    m = best_match(src, [t("Midnight City (Remix)", "Eric Prydz", d=300), t("Midnight City", "M83", d=243)])
    assert m.track.artists == ["M83"]
    assert m.status == "matched"
    assert len(m.candidates) == 2
    assert best_match(src, []).status == "not_found"


def test_search_query_uses_clean_title_and_first_artist():
    assert search_query(t("Song (feat. B) - Remastered 2009", "A", "B")) == "Song A"


def test_parse_duration():
    assert parse_duration("3:45") == 225
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("02:50") == 170
    assert parse_duration("abc") is None
    assert parse_duration(None) is None
