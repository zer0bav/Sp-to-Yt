"""Command line interface. Run `tunebridge` with no arguments to open the terminal UI."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .browser import BrowserError, has_profile, launch_login_window, remove_profile
from .engine import Event, run_transfer
from .models import Playlist
from .presets import Preset, delete_preset, load_presets, save_preset
from .providers import get_provider, registry

app = typer.Typer(
    name="tunebridge",
    help="Move playlists between Spotify, YouTube Music, Deezer, Tidal, Apple Music and CSV/JSON files.",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console(highlight=False)

PlatformArg = Annotated[str, typer.Argument(help="Platform id, see `tunebridge platforms`.")]


def _platform_names() -> list[str]:
    return sorted(registry())


def _check_platform(name: str) -> str:
    if name not in registry():
        raise typer.BadParameter(f"Unknown platform '{name}'. Choose from: {', '.join(_platform_names())}")
    return name


def _fail(msg: str) -> None:
    console.print(f"[bold red]error[/] {msg}")
    raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        console.print(f"tunebridge {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        from .tui.app import run_tui

        run_tui()


# ---- platforms & sessions -----------------------------------------------------------


@app.command()
def platforms() -> None:
    """List supported platforms and whether you are logged in."""
    t = Table(box=None, pad_edge=False, header_style="bold")
    t.add_column("id")
    t.add_column("platform")
    t.add_column("session")
    t.add_column("browser")
    t.add_column("notes", style="dim")
    for name, cls in sorted(registry().items()):
        if not cls.uses_browser:
            session, mode = "not needed", "-"
        else:
            session = "[green]saved[/]" if has_profile(name) else "[yellow]not logged in[/]"
            mode = "background" if cls.headless_ok else "window"
        note = ("[experimental] " if cls.experimental else "") + cls.note
        t.add_row(name, cls.label, session, mode, note)
    console.print(t)


@app.command()
def login(
    platform: PlatformArg,
    verify: Annotated[bool, typer.Option(help="Check the session after the window closes.")] = True,
) -> None:
    """Open a normal browser window to log in. Your password is typed by you, into the real site."""
    _check_platform(platform)
    cls = registry()[platform]
    if not cls.uses_browser:
        console.print(f"{cls.label} does not need a login.")
        return
    console.print(f"Opening [bold]{cls.label}[/] in a new browser window.")
    console.print("Log in as usual (2FA included), then [bold]close the window[/] to continue.")
    try:
        proc = launch_login_window(platform, cls.login_url or cls.home_url)
    except BrowserError as e:
        _fail(str(e))
    with console.status("Waiting for the browser window to close..."):
        proc.wait()
        time.sleep(1.0)
    if not verify:
        console.print("[green]Session saved.[/]")
        return
    with console.status(f"Checking your {cls.label} session..."):
        try:
            with get_provider(platform) as p:
                ok = p.is_logged_in()
        except BrowserError as e:
            _fail(str(e))
    if ok:
        console.print(f"[green]Logged in to {cls.label}.[/]")
    else:
        _fail(f"Could not confirm the {cls.label} login. Run `tunebridge login {platform}` again.")


@app.command()
def logout(platform: PlatformArg) -> None:
    """Delete the saved browser profile (cookies) for a platform."""
    _check_platform(platform)
    if remove_profile(platform):
        console.print(f"Removed the saved {platform} session.")
    else:
        console.print("There was no saved session.")


@app.command()
def status(
    platform: PlatformArg,
    show_browser: Annotated[bool, typer.Option("--show-browser", help="Show the browser window.")] = False,
) -> None:
    """Open the platform in the background and check the login."""
    _check_platform(platform)
    with console.status("Checking..."):
        try:
            with get_provider(platform, show_browser=show_browser) as p:
                ok = p.is_logged_in()
        except BrowserError as e:
            _fail(str(e))
    console.print("[green]Logged in[/]" if ok else f"[yellow]Not logged in.[/] Run `tunebridge login {platform}`.")


@app.command()
def playlists(
    platform: PlatformArg,
    show_browser: Annotated[bool, typer.Option("--show-browser")] = False,
    directory: Annotated[Optional[Path], typer.Option("--dir", help="Folder for the file platform.")] = None,
) -> None:
    """List your playlists on a platform."""
    _check_platform(platform)
    with console.status("Loading playlists..."):
        try:
            with get_provider(platform, show_browser=show_browser, directory=directory) as p:
                p.require_login()
                pls = p.list_playlists()
        except BrowserError as e:
            _fail(str(e))
    _print_playlists(pls)


def _print_playlists(pls: list[Playlist]) -> None:
    t = Table(box=None, header_style="bold")
    t.add_column("#", justify="right", style="dim")
    t.add_column("name")
    t.add_column("tracks", justify="right")
    t.add_column("owner", style="dim")
    for i, p in enumerate(pls, 1):
        t.add_row(str(i), p.name, str(p.track_count) if p.track_count is not None else "", p.owner or "")
    console.print(t)


def _parse_selection(text: str, n: int) -> list[int]:
    text = text.strip().lower()
    if text in ("all", "*", "a"):
        return list(range(n))
    out: list[int] = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    return [i for i in dict.fromkeys(out) if 0 <= i < n]


# ---- transfer --------------------------------------------------------------------------


class _CliReporter:
    """Turns engine events into a live progress display."""

    def __init__(self) -> None:
        self.progress = Progress(
            TextColumn("{task.description}"), BarColumn(bar_width=30), MofNCompleteColumn(), TimeElapsedColumn(),
            console=console, transient=False,
        )
        self.tasks: dict[tuple[str, int], TaskID] = {}
        self.names: dict[int, str] = {}

    def __call__(self, e: Event) -> None:
        d = e.data
        if e.kind == "phase":
            self.progress.console.print(f"\n[bold]{e.message}[/]")
        elif e.kind == "playlist":
            key = (d.get("phase", ""), d.get("index", 0))
            if key not in self.tasks:
                label = {"read": "read ", "match": "match", "write": "write"}.get(key[0], key[0])
                self.tasks[key] = self.progress.add_task(f"{label}  {e.message}", total=d.get("total"))
        elif e.kind == "progress":
            key = (d.get("phase", ""), d.get("index", 0))
            if key in self.tasks:
                self.progress.update(self.tasks[key], completed=d.get("done", 0), total=d.get("total"))
        elif e.kind == "log":
            self.progress.console.print(f"  {e.message}", style="dim")
        elif e.kind == "warn":
            self.progress.console.print(f"  [yellow]![/] {e.message}")
        elif e.kind == "error":
            self.progress.console.print(f"  [red]x[/] {e.message}")


def _do_transfer(
    source: str, target: str, names: list[str], *, all_playlists: bool, include_review: bool, dry_run: bool,
    prefix: str, min_score: float, show_browser: bool, source_options: dict | None = None,
    target_options: dict | None = None,
) -> None:
    rep = _CliReporter()
    with rep.progress:
        try:
            jobs, report = run_transfer(
                source, target, names, all_playlists=all_playlists, include_review=include_review, emit=rep,
                dry_run=dry_run, prefix=prefix, min_score=min_score, show_browser=show_browser,
                source_options=source_options, target_options=target_options,
            )
        except BrowserError as e:
            _fail(str(e))
    if not jobs:
        return
    t = Table(title="Summary", box=None, header_style="bold", title_justify="left")
    for col in ("playlist", "added", "already", "review", "not found", "failed"):
        t.add_column(col, justify="left" if col == "playlist" else "right")
    for j in jobs:
        c = j.counts()
        added = c.get("would_add", 0) if dry_run else c.get("added", 0)
        t.add_row(j.source.name if not j.error else f"{j.source.name} [red](error)[/]", str(added),
                  str(c.get("already", 0)), str(c.get("review", 0)), str(c.get("not_found", 0)), str(c.get("failed", 0)))
    console.print()
    console.print(t)
    if dry_run:
        console.print("[dim]Dry run: nothing was written.[/]")
    if report:
        console.print(f"Report: {report}")
    if any(j.counts().get("review", 0) for j in jobs) and not include_review:
        console.print("[dim]Low-confidence matches were skipped. Use --include-review, or review them in `tunebridge tui`.[/]")


@app.command()
def transfer(
    source: Annotated[str, typer.Option("--from", "-f", help="Source platform.")],
    target: Annotated[str, typer.Option("--to", "-t", help="Target platform.")],
    playlist: Annotated[Optional[list[str]], typer.Option("--playlist", "-p", help="Playlist name (repeatable).")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Move every playlist.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Match only, do not write anything.")] = False,
    include_review: Annotated[bool, typer.Option("--include-review", help="Also add low-confidence matches.")] = False,
    min_score: Annotated[float, typer.Option(help="Score (0-100) needed to add a match automatically.")] = 80.0,
    prefix: Annotated[str, typer.Option(help="Prefix for the new playlist names.")] = "",
    show_browser: Annotated[bool, typer.Option("--show-browser", help="Show the browser windows.")] = False,
    save_as: Annotated[Optional[str], typer.Option("--save-preset", help="Save this selection for `tunebridge sync`.")] = None,
    directory: Annotated[Optional[Path], typer.Option("--dir", help="Folder for the file platform.")] = None,
    fmt: Annotated[str, typer.Option("--format", help="File format when the target is 'file': json or csv.")] = "json",
) -> None:
    """Move playlists from one platform to another."""
    _check_platform(source)
    _check_platform(target)
    names = list(playlist or [])
    file_opts = {"directory": directory, "fmt": fmt}
    if not names and not all_:
        with console.status("Loading playlists..."):
            try:
                with get_provider(source, show_browser=show_browser, **file_opts) as p:
                    p.require_login()
                    pls = p.list_playlists()
            except BrowserError as e:
                _fail(str(e))
        if not pls:
            _fail("No playlists found.")
        _print_playlists(pls)
        picked = _parse_selection(typer.prompt("Select playlists (e.g. 1,3,5-7 or all)"), len(pls))
        if not picked:
            _fail("Nothing selected.")
        names = [pls[i].id for i in picked]
    if save_as:
        save_preset(Preset(name=save_as, source=source, target=target, playlists=names, all_playlists=all_,
                           prefix=prefix, include_review=include_review, min_score=min_score))
        console.print(f"Saved preset [bold]{save_as}[/]. Run it again with `tunebridge sync {save_as}`.")
    _do_transfer(source, target, names, all_playlists=all_, include_review=include_review, dry_run=dry_run,
                 prefix=prefix, min_score=min_score, show_browser=show_browser,
                 source_options=file_opts if source == "file" else None,
                 target_options=file_opts if target == "file" else None)


@app.command()
def sync(
    preset: Annotated[str, typer.Argument(help="Preset name.")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_browser: Annotated[bool, typer.Option("--show-browser")] = False,
) -> None:
    """Re-run a saved preset. Only songs that are not on the target yet are added."""
    presets = load_presets()
    if preset not in presets:
        _fail(f"No preset named '{preset}'. See `tunebridge presets`.")
    p = presets[preset]
    _do_transfer(p.source, p.target, p.playlists, all_playlists=p.all_playlists, include_review=p.include_review,
                 dry_run=dry_run, prefix=p.prefix, min_score=p.min_score, show_browser=show_browser)


@app.command("presets")
def presets_cmd(
    delete: Annotated[Optional[str], typer.Option("--delete", help="Delete the named preset.")] = None,
) -> None:
    """List (or delete) saved presets."""
    if delete:
        console.print("Deleted." if delete_preset(delete) else "No such preset.")
        return
    ps = load_presets()
    if not ps:
        console.print("No presets yet. Save one with `tunebridge transfer ... --save-preset NAME` or from the UI.")
        return
    t = Table(box=None, header_style="bold")
    for col in ("name", "from", "to", "playlists"):
        t.add_column(col)
    for p in ps.values():
        t.add_row(p.name, p.source, p.target, "all" if p.all_playlists else ", ".join(p.playlists))
    console.print(t)


@app.command()
def export(
    platform: PlatformArg,
    playlist: Annotated[Optional[list[str]], typer.Option("--playlist", "-p")] = None,
    all_: Annotated[bool, typer.Option("--all")] = False,
    directory: Annotated[Optional[Path], typer.Option("--dir", help="Output folder (default ~/Music/tunebridge).")] = None,
    fmt: Annotated[str, typer.Option("--format", help="json or csv")] = "json",
    show_browser: Annotated[bool, typer.Option("--show-browser")] = False,
) -> None:
    """Save playlists to JSON/CSV files (backup, or to import elsewhere later)."""
    _check_platform(platform)
    if not playlist and not all_:
        _fail("Pass --playlist NAME (repeatable) or --all.")
    _do_transfer(platform, "file", list(playlist or []), all_playlists=all_, include_review=True, dry_run=False,
                 prefix="", min_score=0, show_browser=show_browser,
                 target_options={"directory": directory, "fmt": fmt})


@app.command("import")
def import_(
    path: Annotated[Path, typer.Argument(help="A .json/.csv file or a folder of them.", exists=True)],
    target: Annotated[str, typer.Option("--to", "-t")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    include_review: Annotated[bool, typer.Option("--include-review")] = False,
    show_browser: Annotated[bool, typer.Option("--show-browser")] = False,
) -> None:
    """Create playlists on a platform from JSON/CSV files."""
    _check_platform(target)
    path = path.resolve()
    folder = path if path.is_dir() else path.parent
    names = [] if path.is_dir() else [str(path)]
    _do_transfer("file", target, names, all_playlists=path.is_dir(), include_review=include_review, dry_run=dry_run,
                 prefix="", min_score=80.0, show_browser=show_browser, source_options={"directory": folder})


# ---- UIs ----------------------------------------------------------------------------------


@app.command()
def tui() -> None:
    """Open the terminal UI."""
    from .tui.app import run_tui

    run_tui()


@app.command()
def web(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the page in your browser.")] = True,
) -> None:
    """Start the web UI on http://127.0.0.1:PORT (only reachable from this computer)."""
    from .web.server import serve

    serve(port=port, open_browser=open_browser)


if __name__ == "__main__":
    app()
