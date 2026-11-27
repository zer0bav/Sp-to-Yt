"""The transfer engine shared by the CLI, the TUI and the web UI.

Two phases:
  1. prepare(): read the chosen source playlists and find each song on the target platform.
  2. apply():   create (or reuse) the target playlists and add the approved matches.

Between the two, a UI can let the user review low-confidence matches.
Progress is reported through `emit(Event)`; the engine itself never prints.
"""

from __future__ import annotations

import csv
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import matcher
from .matcher import MATCHED, REVIEW, best_match, search_query
from .models import Playlist, Track
from .paths import reports_dir
from .providers import Provider, get_provider
from .state import StateStore


@dataclass
class Event:
    kind: str  # phase | playlist | progress | track | log | warn | error | done
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class Cancelled(Exception):
    pass


@dataclass
class TrackResult:
    source: Track
    match: Track | None = None
    score: float = 0.0
    status: str = "pending"  # pending | matched | review | not_found | already | added | failed | would_add
    candidates: list[tuple[Track, float]] = field(default_factory=list)
    approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "match": self.match.to_dict() if self.match else None,
            "score": self.score,
            "status": self.status,
            "approved": self.approved,
            "candidates": [{"track": t.to_dict(), "score": s} for t, s in self.candidates],
        }


@dataclass
class PlaylistJob:
    source: Playlist
    target_name: str
    results: list[TrackResult] = field(default_factory=list)
    target: Playlist | None = None
    error: str | None = None

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        c["total"] = len(self.results)
        c["approved"] = sum(1 for r in self.results if r.approved and r.status in ("matched", "review"))
        return c

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "target_name": self.target_name,
            "target": self.target.to_dict() if self.target else None,
            "error": self.error,
            "counts": self.counts(),
            "results": [r.to_dict() for r in self.results],
        }


class TransferEngine:
    def __init__(
        self,
        source: str,
        target: str,
        *,
        emit: Callable[[Event], None] | None = None,
        store: StateStore | None = None,
        show_browser: bool = False,
        dry_run: bool = False,
        prefix: str = "",
        min_score: float = MATCHED,
        review_score: float = REVIEW,
        source_options: dict[str, Any] | None = None,
        target_options: dict[str, Any] | None = None,
    ) -> None:
        if source == target and source != "file":
            raise ValueError("Source and target must be different platforms.")
        self.source_name = source
        self.target_name = target
        self.emit = emit or (lambda e: None)
        self.store = store or StateStore()
        self.show_browser = show_browser
        self.dry_run = dry_run
        self.prefix = prefix
        self.min_score = min_score
        self.review_score = review_score
        self.source_options = source_options or {}
        self.target_options = target_options or {}
        self._source: Provider | None = None
        self._target: Provider | None = None
        self.cancel_event = threading.Event()
        self.report_path: Path | None = None
        self.jobs: list[PlaylistJob] = []

    # ---- providers ---------------------------------------------------------------

    @property
    def source(self) -> Provider:
        if self._source is None:
            self._source = get_provider(self.source_name, show_browser=self.show_browser, **self.source_options)
        return self._source

    @property
    def target(self) -> Provider:
        if self._target is None:
            self._target = get_provider(self.target_name, show_browser=self.show_browser, **self.target_options)
        return self._target

    def close(self) -> None:
        for p in (self._source, self._target):
            if p is not None:
                p.close()
        self._source = self._target = None

    def cancel(self) -> None:
        self.cancel_event.set()

    def _check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def _log(self, msg: str, kind: str = "log", **data: Any) -> None:
        self.emit(Event(kind, msg, data))

    # ---- phase 1 -------------------------------------------------------------------

    def list_source_playlists(self) -> list[Playlist]:
        self._log(f"Opening {self.source.label}...")
        self.source.require_login()
        pls = self.source.list_playlists()
        self._log(f"Found {len(pls)} playlists on {self.source.label}.")
        return pls

    def prepare(self, playlists: list[Playlist]) -> list[PlaylistJob]:
        jobs = [PlaylistJob(source=p, target_name=f"{self.prefix}{p.name}") for p in playlists]
        self.jobs = jobs
        self.emit(Event("phase", f"Reading {len(jobs)} playlist(s) from {self.source.label}", {"phase": "read"}))
        if self.source.uses_browser:
            self.source.require_login()
        for i, job in enumerate(jobs):
            self._check()
            self.emit(Event("playlist", f"Reading '{job.source.name}'", {"index": i, "phase": "read"}))

            def prog(done: int, total: int | None, i: int = i) -> None:
                self.emit(Event("progress", "", {"index": i, "phase": "read", "done": done, "total": total}))

            try:
                tracks = self.source.get_tracks(job.source, progress=prog)
            except Exception as e:  # keep going with the other playlists
                job.error = f"Could not read playlist: {e}"
                self._log(job.error, "error", index=i)
                continue
            job.results = [TrackResult(source=t) for t in tracks]
            self._log(f"'{job.source.name}': {len(tracks)} tracks", index=i)
        # the source browser is no longer needed; free its memory before opening the target
        if self._source is not None and self.source_name != self.target_name:
            self._source.close()
            self._source = None

        self.emit(Event("phase", f"Matching songs on {self.target.label}", {"phase": "match"}))
        if self.target.uses_browser:
            self.target.require_login()
        for i, job in enumerate(jobs):
            if job.error:
                continue
            already = self.store.added_keys(self.source_name, job.source.id, self.target_name)
            total = len(job.results)
            self.emit(Event("playlist", f"Matching '{job.source.name}'", {"index": i, "phase": "match", "total": total}))
            for n, r in enumerate(job.results):
                self._check()
                if r.source.key() in already:
                    r.status = "already"
                elif self.target.exact_copy:
                    r.match, r.score, r.status, r.approved = r.source, 100.0, "matched", True
                else:
                    self._match_one(r)
                self.emit(Event("track", "", {"index": i, "pos": n, "result": r.to_dict()}))
                self.emit(Event("progress", "", {"index": i, "phase": "match", "done": n + 1, "total": total}))
            c = job.counts()
            self._log(
                f"'{job.source.name}': {c.get('matched', 0)} matched, {c.get('review', 0)} to review, "
                f"{c.get('not_found', 0)} not found, {c.get('already', 0)} already moved",
                index=i,
            )
        self.emit(Event("phase", "Ready to transfer", {"phase": "review"}))
        return jobs

    def _match_one(self, r: TrackResult) -> None:
        try:
            cands = self.target.search(r.source)
            m = best_match(r.source, cands)
            if m.score < self.min_score:
                # second try with the title only; helps when the artist name is spelled differently
                alt = r.source.title if r.source.artists else matcher.clean_title(r.source.title)
                if alt and alt != search_query(r.source):
                    cands2 = self.target.search(Track(title=alt, artists=[], duration=r.source.duration))
                    m2 = best_match(r.source, cands + cands2)
                    if m2.score > m.score:
                        m = m2
        except Exception as e:
            r.status = "failed"
            self._log(f"Search failed for '{r.source.label()}': {e}", "warn")
            return
        r.match, r.score, r.candidates = m.track, m.score, m.candidates
        if m.track is None or m.score < self.review_score:
            r.status = "not_found"
        elif m.score >= self.min_score:
            r.status, r.approved = "matched", True
        else:
            r.status, r.approved = "review", False

    # ---- phase 2 -------------------------------------------------------------------

    def apply(self, jobs: list[PlaylistJob]) -> Path:
        self.emit(Event("phase", f"Writing to {self.target.label}", {"phase": "write"}))
        for i, job in enumerate(jobs):
            self._check()
            if job.error:
                continue
            todo = [r for r in job.results if r.approved and r.match is not None and r.status in ("matched", "review")]
            self.emit(Event("playlist", f"'{job.target_name}': {len(todo)} to add", {"index": i, "phase": "write", "total": len(todo)}))
            if self.dry_run:
                for r in todo:
                    r.status = "would_add"
                self.emit(Event("progress", "", {"index": i, "phase": "write", "done": len(todo), "total": len(todo)}))
                self._log(f"[dry run] would add {len(todo)} tracks to '{job.target_name}'", index=i)
                continue
            if not todo:
                mapped = self.store.target_playlist(self.source_name, job.source.id, self.target_name)
                self._log(f"'{job.target_name}' is already up to date." if mapped
                          else f"'{job.target_name}': nothing to add, playlist not created.", index=i)
                continue
            try:
                self._write_job(i, job, todo)
            except Cancelled:
                raise
            except Exception as e:
                job.error = f"Could not write playlist: {e}"
                self._log(job.error, "error", index=i)
        self.report_path = self.write_report(jobs)
        totals = self.summary(jobs)
        self.emit(Event("done", "Finished", {"report": str(self.report_path), "summary": totals}))
        return self.report_path

    def _resolve_target(self, job: PlaylistJob) -> tuple[Playlist, bool]:
        mapped = self.store.target_playlist(self.source_name, job.source.id, self.target_name)
        if mapped:
            return Playlist(id=mapped[0], name=mapped[1]), True
        if not self.target.exact_copy:
            existing = self.target.find_playlist(job.target_name)
            if existing:
                return existing, True
        pl = self.target.create_playlist(job.target_name, f"Moved from {self.source.label} with TuneBridge")
        return pl, False

    def _write_job(self, i: int, job: PlaylistJob, todo: list[TrackResult]) -> None:
        target_pl, existed = self._resolve_target(job)
        job.target = target_pl
        self.store.set_target_playlist(self.source_name, job.source.id, self.target_name, target_pl.id, target_pl.name)
        self._log(f"{'Using existing' if existed else 'Created'} playlist '{target_pl.name}' on {self.target.label}", index=i)
        if existed and not self.target.exact_copy and todo:
            # never add a song twice, even if it was added by hand
            present = {t.id for t in self.target.get_tracks(target_pl) if t.id}
            for r in todo:
                if r.match and r.match.id in present:
                    r.status = "already"
            self.store.mark_added(
                self.source_name, job.source.id, self.target_name,
                [(r.source.key(), r.match.id) for r in todo if r.status == "already" and r.match],
            )
            todo = [r for r in todo if r.status != "already"]

        done = 0

        def prog(n: int, total: int | None) -> None:
            self.emit(Event("progress", "", {"index": i, "phase": "write", "done": n, "total": total or len(todo)}))

        # write in small batches so state is saved as we go and a crash loses little
        batch = 25
        for start in range(0, len(todo), batch):
            self._check()
            chunk = todo[start : start + batch]
            added = self.target.add_tracks(target_pl, [r.match for r in chunk if r.match], progress=lambda n, t: prog(done + n, len(todo)))
            added_ids = {id(t) for t in added}
            ok: list[tuple[str, str | None]] = []
            for r in chunk:
                if r.match is not None and id(r.match) in added_ids:
                    r.status = "added"
                    ok.append((r.source.key(), r.match.id))
                else:
                    r.status = "failed"
            self.store.mark_added(self.source_name, job.source.id, self.target_name, ok)
            done += len(chunk)
            prog(done, len(todo))
        c = job.counts()
        self._log(f"'{target_pl.name}': added {c.get('added', 0)}, failed {c.get('failed', 0)}", index=i)

    # ---- reporting -----------------------------------------------------------------

    @staticmethod
    def summary(jobs: list[PlaylistJob]) -> dict[str, int]:
        out: dict[str, int] = {"playlists": len(jobs)}
        for j in jobs:
            for k, v in j.counts().items():
                out[k] = out.get(k, 0) + v
        return out

    def write_report(self, jobs: list[PlaylistJob]) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = reports_dir() / f"{stamp}-{self.source_name}-to-{self.target_name}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["playlist", "status", "score", "title", "artists", "match_title", "match_artists", "match_id"])
            for j in jobs:
                if j.error:
                    w.writerow([j.source.name, "error", "", j.error, "", "", "", ""])
                for r in j.results:
                    m = r.match
                    w.writerow([
                        j.source.name, r.status, r.score, r.source.title, r.source.artist_line,
                        m.title if m else "", m.artist_line if m else "", m.id if m else "",
                    ])
        return path


def run_transfer(
    source: str,
    target: str,
    playlist_names: list[str] | None,
    *,
    all_playlists: bool = False,
    include_review: bool = False,
    emit: Callable[[Event], None] | None = None,
    **engine_kw: Any,
) -> tuple[list[PlaylistJob], Path | None]:
    """Non-interactive helper used by `tunebridge transfer` and `tunebridge sync`."""
    eng = TransferEngine(source, target, emit=emit, **engine_kw)
    try:
        pls = eng.list_source_playlists()
        if all_playlists:
            chosen = pls
        else:
            wanted = {n.strip().lower() for n in playlist_names or []}
            chosen = [p for p in pls if p.name.strip().lower() in wanted or p.id in (playlist_names or [])]
            missing = wanted - {p.name.strip().lower() for p in chosen} - {p.id.lower() for p in chosen}
            for m in sorted(missing):
                eng._log(f"Playlist not found on {eng.source.label}: {m}", "warn")
        if not chosen:
            eng._log("Nothing to transfer.", "warn")
            return [], None
        t0 = time.monotonic()
        jobs = eng.prepare(chosen)
        if include_review:
            for j in jobs:
                for r in j.results:
                    if r.status == "review":
                        r.approved = True
        report = eng.apply(jobs)
        eng._log(f"Done in {time.monotonic() - t0:.0f}s")
        return jobs, report
    finally:
        eng.close()
