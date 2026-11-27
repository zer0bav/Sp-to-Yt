import time

from fastapi.testclient import TestClient

from tunebridge.web.server import create_app

H = {"X-TuneBridge": "1"}


def client():
    return TestClient(create_app(8765), base_url="http://127.0.0.1:8765")


def wait_state(c, job_id, states, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["state"] in states:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {j['state']}")


def test_guards_block_foreign_hosts_and_csrf():
    c = client()
    assert c.get("/api/platforms", headers={"Host": "evil.example"}).status_code == 403
    assert c.post("/api/jobs", json={}).status_code == 403  # no X-TuneBridge header
    r = c.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_full_flow_with_review_and_report():
    c = client()
    ids = {p["id"] for p in c.get("/api/platforms").json()}
    assert {"spotify", "ytmusic", "file", "demo", "demo2"} <= ids
    pls = c.get("/api/platforms/demo/playlists").json()
    r = c.post("/api/jobs", headers=H, json={"source": "demo", "target": "demo2", "playlists": pls[:3], "preset_name": "web"})
    job_id = r.json()["id"]
    j = wait_state(c, job_id, {"review"})
    assert j["summary"]["total"] == 19
    review = [(pi, ri) for pi, pl in enumerate(j["playlists"]) for ri, x in enumerate(pl["results"]) if x["status"] == "review"]
    if review:
        pi, ri = review[0]
        s = c.post(f"/api/jobs/{job_id}/decisions", headers=H,
                   json={"items": [{"playlist": pi, "index": ri, "approved": True, "candidate": 0}]}).json()
        assert s["summary"]["approved"] == j["summary"]["approved"] + 1
    assert c.post(f"/api/jobs/{job_id}/apply", headers=H).status_code == 200
    j = wait_state(c, job_id, {"done", "error"})
    assert j["state"] == "done" and j["summary"]["added"] >= 16
    rep = c.get(f"/api/jobs/{job_id}/report")
    assert rep.status_code == 200 and rep.text.startswith("playlist,status")
    assert any(p["name"] == "web" for p in c.get("/api/presets").json())


def test_preset_run_is_incremental():
    c = client()
    pls = c.get("/api/platforms/demo/playlists").json()
    first = c.post("/api/jobs", headers=H, json={"source": "demo", "target": "demo2", "playlists": pls[1:2], "preset_name": "focus"}).json()["id"]
    wait_state(c, first, {"review"})
    c.post(f"/api/jobs/{first}/apply", headers=H)
    wait_state(c, first, {"done"})
    again = c.post("/api/presets/focus/run", headers=H).json()["id"]
    j = wait_state(c, again, {"done", "error"})
    assert j["state"] == "done"
    assert j["summary"].get("added", 0) == 0 and j["summary"]["already"] == 5


def test_rejects_same_source_and_target():
    c = client()
    r = c.post("/api/jobs", headers=H, json={"source": "demo", "target": "demo", "playlists": [{"id": "p1", "name": "x"}]})
    assert r.status_code == 400
