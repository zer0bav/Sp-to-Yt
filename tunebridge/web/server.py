"""Local web UI. Binds to 127.0.0.1 only.

The page talks to a small JSON API; long-running work happens in threads and
streams progress to the page with Server-Sent Events.

Hardening (the server drives your logged-in browser sessions, so treat it as sensitive):
  * only listens on 127.0.0.1
  * rejects requests whose Host header is not 127.0.0.1/localhost (DNS rebinding)
  * state-changing requests must carry the X-TuneBridge header, which a cross-site
    form or image cannot send (CSRF)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..browser import BrowserError, has_profile, launch_login_window, remove_profile
from ..engine import Cancelled, Event, PlaylistJob, TransferEngine
from ..matcher import MATCHED
from ..models import Playlist
from ..presets import Preset, delete_preset, load_presets, save_preset
from ..providers import get_provider, registry

STATIC = Path(__file__).with_name("static")


# ---- job model -----------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    source: str
    target: str
    playlists: list[Playlist]
    dry_run: bool = False
    auto_apply: bool = False
    include_review: bool = False
    state: str = "starting"  # starting | read | match | review | write | done | error | cancelled
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[PlaylistJob] = field(default_factory=list)
    report: str | None = None
    engine: TransferEngine | None = None
    created: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, kind: str, message: str = "", **data: Any) -> None:
        with self.lock:
            self.events.append({"seq": len(self.events), "kind": kind, "message": message, "data": data})

    def snapshot(self, results: bool = True) -> dict[str, Any]:
        jobs = self.engine.jobs if (self.engine and not self.jobs) else self.jobs
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "state": self.state,
            "error": self.error,
            "dry_run": self.dry_run,
            "report": bool(self.report),
            "playlists": [j.to_dict() if results else {**j.to_dict(), "results": []} for j in jobs],
            "summary": TransferEngine.summary(jobs) if jobs else {},
        }


class JobIn(BaseModel):
    source: str
    target: str
    playlists: list[dict[str, Any]]
    dry_run: bool = False
    prefix: str = ""
    min_score: float = MATCHED
    preset_name: str = ""

class Decision(BaseModel):
    playlist: int
    index: int
    approved: bool
    candidate: int | None = None

class Decisions(BaseModel):
    items: list[Decision]


JOBS: dict[str, Job] = {}
LOGIN: dict[str, dict[str, str]] = {}  # platform -> {"state": waiting|checking|ok|failed, "message": ...}


def _emit_for(job: Job):
    def emit(e: Event) -> None:
        if e.kind == "phase" and e.data.get("phase") in ("read", "match", "write"):
            job.state = e.data["phase"]
        data = dict(e.data)
        if e.kind == "track":
            # results are large; the page fetches them from the snapshot instead
            data = {"index": data.get("index"), "pos": data.get("pos"), "status": data["result"]["status"],
                    "score": data["result"]["score"]}
        job.push(e.kind, e.message, **data)

    return emit


def _run_prepare(job: Job) -> None:
    try:
        job.jobs = job.engine.prepare(job.playlists)  # type: ignore[union-attr]
        job.state = "review"
        job.push("state", "review")
        if job.auto_apply:
            if job.include_review:
                for j in job.jobs:
                    for r in j.results:
                        if r.status == "review" and r.match is not None:
                            r.approved = True
            _run_apply(job)
    except Cancelled:
        job.state = "cancelled"
        job.push("state", "cancelled")
        _close(job)
    except Exception as e:  # report every failure to the page
        job.state, job.error = "error", str(e)
        job.push("error", str(e))
        job.push("state", "error")
        _close(job)


def _run_apply(job: Job) -> None:
    try:
        job.state = "write"
        job.push("state", "write")
        path = job.engine.apply(job.jobs)  # type: ignore[union-attr]
        job.report = str(path)
        job.state = "done"
        job.push("state", "done")
    except Cancelled:
        job.state = "cancelled"
        job.push("state", "cancelled")
    except Exception as e:
        job.state, job.error = "error", str(e)
        job.push("error", str(e))
        job.push("state", "error")
    finally:
        _close(job)


def _close(job: Job) -> None:
    if job.engine is not None:
        try:
            job.engine.close()
        except Exception:
            pass


# ---- app -----------------------------------------------------------------------------------


def create_app(port: int = 8765) -> FastAPI:
    app = FastAPI(title="TuneBridge", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-tunebridge") != "1":
            return JSONResponse({"detail": "Missing X-TuneBridge header"}, status_code=403)
        resp: Response = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-cache")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"
        )
        return resp

    # ---- platforms ---------------------------------------------------------------------

    @app.get("/api/platforms")
    def platforms() -> list[dict[str, Any]]:
        out = []
        for name, cls in sorted(registry().items(), key=lambda kv: kv[1].label):
            out.append({
                "id": name,
                "label": cls.label,
                "uses_browser": cls.uses_browser,
                "headless": cls.headless_ok,
                "experimental": cls.experimental,
                "note": cls.note,
                "session": (not cls.uses_browser) or has_profile(name),
                "login": LOGIN.get(name, {}),
            })
        return out

    def _platform(name: str):
        reg = registry()
        if name not in reg:
            raise HTTPException(404, f"Unknown platform {name}")
        return reg[name]

    def _check_thread(name: str) -> None:
        cls = _platform(name)
        LOGIN[name] = {"state": "checking", "message": f"Checking your {cls.label} session..."}
        try:
            with get_provider(name) as p:
                ok = p.is_logged_in()
            LOGIN[name] = {"state": "ok" if ok else "failed",
                           "message": f"Logged in to {cls.label}." if ok else f"Not logged in to {cls.label}."}
        except BrowserError as e:
            LOGIN[name] = {"state": "failed", "message": str(e)}

    @app.post("/api/platforms/{name}/login")
    def login(name: str) -> dict[str, Any]:
        cls = _platform(name)
        if not cls.uses_browser:
            return {"state": "ok", "message": "No login needed."}
        try:
            proc = launch_login_window(name, cls.login_url or cls.home_url)
        except BrowserError as e:
            raise HTTPException(409, str(e)) from e
        LOGIN[name] = {"state": "waiting", "message": f"Log in to {cls.label} in the new window, then close it."}

        def wait() -> None:
            proc.wait()
            time.sleep(1.0)
            _check_thread(name)

        threading.Thread(target=wait, daemon=True).start()
        return LOGIN[name]

    @app.post("/api/platforms/{name}/check")
    def check(name: str) -> dict[str, Any]:
        _platform(name)
        threading.Thread(target=_check_thread, args=(name,), daemon=True).start()
        return {"state": "checking"}

    @app.delete("/api/platforms/{name}/session")
    def forget(name: str) -> dict[str, Any]:
        _platform(name)
        LOGIN.pop(name, None)
        return {"removed": remove_profile(name)}

    @app.get("/api/platforms/{name}/playlists")
    def playlists(name: str) -> list[dict[str, Any]]:
        cls = _platform(name)
        try:
            with get_provider(name) as p:
                if cls.uses_browser and not p.is_logged_in():
                    raise HTTPException(401, f"Not logged in to {cls.label}.")
                return [pl.to_dict() for pl in p.list_playlists()]
        except BrowserError as e:
            raise HTTPException(502, str(e)) from e

    # ---- jobs --------------------------------------------------------------------------

    def _start(job: Job, prefix: str, min_score: float) -> None:
        job.engine = TransferEngine(job.source, job.target, emit=_emit_for(job), dry_run=job.dry_run,
                                    prefix=prefix, min_score=min_score)
        JOBS[job.id] = job
        threading.Thread(target=_run_prepare, args=(job,), daemon=True).start()

    @app.post("/api/jobs")
    def create_job(body: JobIn) -> dict[str, Any]:
        _platform(body.source)
        _platform(body.target)
        if body.source == body.target:
            raise HTTPException(400, "Source and target must differ.")
        pls = [Playlist.from_dict(p) for p in body.playlists]
        if not pls:
            raise HTTPException(400, "Select at least one playlist.")
        if body.preset_name.strip():
            save_preset(Preset(name=body.preset_name.strip(), source=body.source, target=body.target,
                               playlists=[p.name for p in pls], prefix=body.prefix, min_score=body.min_score))
        job = Job(id=uuid.uuid4().hex[:12], source=body.source, target=body.target, playlists=pls, dry_run=body.dry_run)
        _start(job, body.prefix, body.min_score)
        return {"id": job.id}

    def _job(job_id: str) -> Job:
        if job_id not in JOBS:
            raise HTTPException(404, "No such job.")
        return JOBS[job_id]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return _job(job_id).snapshot()

    @app.post("/api/jobs/{job_id}/decisions")
    def decide(job_id: str, body: Decisions) -> dict[str, Any]:
        job = _job(job_id)
        if job.state != "review":
            raise HTTPException(409, "Matches can only be changed while reviewing.")
        for d in body.items:
            try:
                r = job.jobs[d.playlist].results[d.index]
            except IndexError:
                raise HTTPException(400, "Bad index.") from None
            if d.candidate is not None:
                if not 0 <= d.candidate < len(r.candidates):
                    raise HTTPException(400, "Bad candidate.")
                r.match, r.score = r.candidates[d.candidate]
                if r.status == "not_found":
                    r.status = "review"
            if r.match is not None and r.status in ("matched", "review", "not_found"):
                if r.status == "not_found" and d.approved:
                    r.status = "review"
                r.approved = d.approved
        return {"summary": TransferEngine.summary(job.jobs)}

    @app.post("/api/jobs/{job_id}/apply")
    def apply(job_id: str) -> dict[str, Any]:
        job = _job(job_id)
        if job.state != "review":
            raise HTTPException(409, f"Job is {job.state}.")
        job.state = "write"
        threading.Thread(target=_run_apply, args=(job,), daemon=True).start()
        return {"state": job.state}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, Any]:
        job = _job(job_id)
        if job.engine is not None:
            job.engine.cancel()
        if job.state == "review":
            job.state = "cancelled"
            job.push("state", "cancelled")
            threading.Thread(target=_close, args=(job,), daemon=True).start()
        return {"state": job.state}

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, after: int = -1) -> StreamingResponse:
        job = _job(job_id)

        async def stream():
            pos = after + 1
            idle = 0
            while True:
                if await request.is_disconnected():
                    return
                with job.lock:
                    batch = job.events[pos:]
                for ev in batch:
                    yield f"id: {ev['seq']}\nevent: tb\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                pos += len(batch)
                if job.state in ("done", "error", "cancelled") and not batch:
                    yield "event: end\ndata: {}\n\n"
                    return
                idle = 0 if batch else idle + 1
                if idle and idle % 75 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str) -> FileResponse:
        job = _job(job_id)
        if not job.report:
            raise HTTPException(404, "No report yet.")
        return FileResponse(job.report, media_type="text/csv", filename=Path(job.report).name)

    # ---- presets -----------------------------------------------------------------------

    @app.get("/api/presets")
    def presets() -> list[dict[str, Any]]:
        return [p.to_dict() for p in load_presets().values()]

    @app.delete("/api/presets/{name}")
    def remove_preset(name: str) -> dict[str, Any]:
        return {"removed": delete_preset(name)}

    @app.post("/api/presets/{name}/run")
    def run_preset(name: str, dry_run: bool = False) -> dict[str, Any]:
        ps = load_presets()
        if name not in ps:
            raise HTTPException(404, "No such preset.")
        p = ps[name]
        # resolve playlist names on the source first (in the job thread, so the request returns at once)
        job = Job(id=uuid.uuid4().hex[:12], source=p.source, target=p.target, playlists=[], dry_run=dry_run,
                  auto_apply=True, include_review=p.include_review)

        def resolve_and_run() -> None:
            try:
                job.engine = TransferEngine(p.source, p.target, emit=_emit_for(job), dry_run=dry_run,
                                            prefix=p.prefix, min_score=p.min_score)
                pls = job.engine.list_source_playlists()
                if p.all_playlists:
                    job.playlists = pls
                else:
                    wanted = {n.lower() for n in p.playlists}
                    job.playlists = [x for x in pls if x.name.lower() in wanted or x.id in p.playlists]
                if not job.playlists:
                    raise RuntimeError("None of the preset's playlists were found on the source.")
                _run_prepare(job)
            except Exception as e:
                job.state, job.error = "error", str(e)
                job.push("error", str(e))
                job.push("state", "error")
                _close(job)

        JOBS[job.id] = job
        threading.Thread(target=resolve_and_run, daemon=True).start()
        return {"id": job.id}

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {"version": __version__}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def serve(port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    print(f"TuneBridge web UI: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, log_level="warning")
