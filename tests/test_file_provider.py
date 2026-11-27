import json

from tunebridge.models import Track
from tunebridge.providers import get_provider


def test_json_round_trip(tmp_path):
    p = get_provider("file", directory=tmp_path, fmt="json")
    pl = p.create_playlist("Road Trip / 2026")
    tracks = [Track("Song A", ["Artist 1", "Artist 2"], album="Al", duration=200, isrc="X1"), Track("Song B", ["C"])]
    assert len(p.add_tracks(pl, tracks)) == 2
    listed = p.list_playlists()
    assert [x.name for x in listed] == ["Road Trip / 2026"]
    back = p.get_tracks(listed[0])
    assert back[0].artists == ["Artist 1", "Artist 2"] and back[0].isrc == "X1" and back[0].duration == 200
    data = json.loads((tmp_path / "Road_Trip__2026.json").read_text())
    assert data["generator"] == "tunebridge"


def test_csv_round_trip(tmp_path):
    p = get_provider("file", directory=tmp_path, fmt="csv")
    pl = p.create_playlist("Mix")
    p.add_tracks(pl, [Track("Şarkı", ["Duman"], duration=233)])
    back = p.get_tracks(p.list_playlists()[0])
    assert back[0].title == "Şarkı" and back[0].artists == ["Duman"] and back[0].duration == 233


def test_reads_hand_written_csv(tmp_path):
    (tmp_path / "mine.csv").write_text("Title,Artists,Duration\nHello,Adele,4:55\n", encoding="utf-8")
    p = get_provider("file", directory=tmp_path)
    tracks = p.get_tracks(p.list_playlists()[0])
    assert tracks[0].title == "Hello" and tracks[0].duration == 295
