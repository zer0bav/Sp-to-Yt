import csv

import pytest

from tunebridge.engine import Cancelled, TransferEngine, run_transfer
from tunebridge.presets import Preset, delete_preset, load_presets, save_preset


def test_full_transfer_and_report():
    events = []
    jobs, report = run_transfer("demo", "demo2", ["Late Night Drive", "Türkçe Rock"], emit=events.append)
    assert len(jobs) == 2
    c = TransferEngine.summary(jobs)
    assert c["added"] >= 9
    assert c["not_found"] == 1  # "Unknown Demo Track"
    assert any(e.kind == "done" for e in events)
    rows = list(csv.DictReader(report.open(encoding="utf-8")))
    assert {r["status"] for r in rows} >= {"added", "not_found"}


def test_dry_run_writes_nothing():
    eng = TransferEngine("demo", "demo2", dry_run=True)
    jobs = eng.prepare([p for p in eng.source.list_playlists() if p.id == "p2"])
    eng.apply(jobs)
    assert all(r.status in ("would_add", "review", "not_found") for r in jobs[0].results)
    assert eng.target.list_playlists() == []


def test_second_run_only_adds_new_songs():
    run_transfer("demo", "demo2", ["Focus"])
    jobs, _ = run_transfer("demo", "demo2", ["Focus"])
    statuses = {r.status for r in jobs[0].results}
    assert "added" not in statuses
    assert "already" in statuses


def test_include_review_adds_uncertain_matches():
    jobs, _ = run_transfer("demo", "demo2", ["Türkçe Rock"], include_review=True)
    assert all(r.status != "review" for r in jobs[0].results)


def test_unknown_playlist_is_reported():
    events = []
    jobs, report = run_transfer("demo", "demo2", ["Does Not Exist"], emit=events.append)
    assert jobs == [] and report is None
    assert any(e.kind == "warn" and "not found" in e.message.lower() for e in events)


def test_cancel_stops_the_engine():
    eng = TransferEngine("demo", "demo2")
    eng.cancel()
    with pytest.raises(Cancelled):
        eng.prepare(eng.source.list_playlists()[:1])


def test_same_platform_is_rejected():
    with pytest.raises(ValueError):
        TransferEngine("demo", "demo")


def test_export_to_file(tmp_path):
    jobs, _ = run_transfer("demo", "file", ["Focus"], target_options={"directory": tmp_path, "fmt": "json"})
    assert jobs[0].counts()["added"] == 5
    assert (tmp_path / "Focus.json").exists()


def test_presets_round_trip():
    save_preset(Preset(name="weekly", source="demo", target="demo2", playlists=["Focus"]))
    assert load_presets()["weekly"].playlists == ["Focus"]
    assert delete_preset("weekly") and "weekly" not in load_presets()
