from tunebridge.providers.deezer import _to_track as deezer_row
from tunebridge.providers.spotify import _count, _owner, _row_to_track as spotify_row
from tunebridge.providers.ytmusic import _row_to_track as yt_row


def test_spotify_row():
    t = spotify_row({"title": "Babydoll", "href": "/track/1o9mAOmNm3wT2NdyGV1Ard", "artists": ["Jamie Miller"],
                     "album": "Babydoll", "duration": "2:58"})
    assert t.id == "1o9mAOmNm3wT2NdyGV1Ard" and t.duration == 178 and t.url.endswith(t.id)
    assert spotify_row({"title": ""}) is None


def test_spotify_library_subtitles():
    assert _count("Playlist • 25 songs") == 25
    assert _count("Playlist • 1,204 songs") == 1204
    assert _owner("Playlist • zer0bav") == "zer0bav"
    assert _owner("Playlist • 25 songs") is None


def test_ytmusic_search_row_with_unlinked_artist():
    t = yt_row({"title": "BbY WOW", "video": "ljAH6jNaApU", "duration": "3:46",
                "cols": [{"text": "KAROL G\n, \nJudeline\n & \nrusowsky • BbY WOW • 3:46", "artists": ["KAROL G", "rusowsky"]}]})
    assert set(t.artists) == {"KAROL G", "Judeline", "rusowsky"}
    assert t.album == "BbY WOW" and t.duration == 226 and t.id == "ljAH6jNaApU"


def test_ytmusic_keeps_band_names_with_ampersand():
    t = yt_row({"title": "September", "video": "y", "duration": "3:35",
                "cols": [{"text": "Earth, Wind & Fire", "artists": ["Earth, Wind & Fire"]}, {"text": "The Best of", "artists": []}]})
    assert t.artists == ["Earth, Wind & Fire"] and t.album == "The Best of"


def test_ytmusic_drops_type_and_play_count():
    t = yt_row({"title": "Babydoll", "video": "x", "duration": None,
                "cols": [{"text": "Song • Jamie Miller • 37M plays", "artists": ["Jamie Miller"]}]})
    assert t.artists == ["Jamie Miller"] and t.album is None


def test_deezer_row_strips_position_number():
    t = deezer_row({"title": "2. Boston", "artists": ["STELLA LEFTY"], "album": "Boston", "duration": "02:50"})
    assert t.title == "Boston" and t.duration == 170
