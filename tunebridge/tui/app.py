"""Terminal UI (Textual).

Flow: Connect -> Playlists -> Review -> Transfer -> Report.
All browser work happens in worker threads; events from the engine are
forwarded to the UI thread with call_from_thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    ProgressBar,
    RichLog,
    SelectionList,
    Static,
    Switch,
)
from textual.widgets.option_list import Option

from ..browser import BrowserError, has_profile, launch_login_window, remove_profile
from ..engine import Cancelled, Event, PlaylistJob, TrackResult, TransferEngine
from ..models import Playlist, Track, fmt_duration
from ..presets import Preset, save_preset
from ..providers import get_provider, registry

STEPS = ["Connect", "Playlists", "Review", "Transfer", "Report"]

DARK = Theme(
    name="tunebridge-dark",
    primary="#E0A458",
    secondary="#8FA8C8",
    accent="#E0A458",
    warning="#D9B44A",
    error="#E06C75",
    success="#7FB069",
    foreground="#D8DEE4",
    background="#101214",
    surface="#16191D",
    panel="#1C2025",
    dark=True,
    variables={"footer-key-foreground": "#E0A458", "block-cursor-background": "#2A2F36",
               "block-cursor-foreground": "#F2F4F7", "block-cursor-text-style": "none"},
)
LIGHT = Theme(
    name="tunebridge-light",
    primary="#A65F00",
    secondary="#3D5A80",
    accent="#A65F00",
    warning="#9A6B00",
    error="#B3261E",
    success="#2F7D32",
    foreground="#1F2328",
    background="#F6F5F2",
    surface="#FFFFFF",
    panel="#ECEAE4",
    dark=False,
    variables={"footer-key-foreground": "#A65F00", "block-cursor-background": "#E4E0D6",
               "block-cursor-foreground": "#1F2328", "block-cursor-text-style": "none"},
)

# status -> (label, style). Plain words instead of icons so they read well in any terminal.
BADGES: dict[str, tuple[str, str]] = {
    "pending": ("....", "dim"),
    "matched": ("MATCH", "bold $success"),
    "review": ("CHECK", "bold $warning"),
    "not_found": ("NONE", "bold $error"),
    "already": ("KEPT", "dim"),
    "added": ("ADDED", "bold $success"),
    "failed": ("FAIL", "bold $error"),
    "would_add": ("DRY", "bold $secondary"),
    "skipped": ("SKIP", "dim"),
}


def badge(app: App, r: TrackResult) -> Text:
    status = r.status
    if status == "review" and r.approved:
        label, style = "OK", "bold $success"
    elif status == "review":
        label, style = BADGES["review"]
    else:
        label, style = BADGES.get(status, (status.upper(), ""))
    style = _resolve(app, style)
    return Text(f"{label:<5}", style=style)


def _resolve(app: App, style: str) -> str:
    """Swap $variables in a style for the current theme's colors."""
    if "$" not in style:
        return style
    vars_ = app.theme_variables
    return " ".join(vars_.get(p[1:], p) if p.startswith("$") else p for p in style.split())


def score_text(app: App, r: TrackResult) -> Text:
    if r.match is None or r.status in ("already", "pending"):
        return Text("")
    s = r.score
    style = "$success" if s >= 80 else "$warning" if s >= 60 else "$error"
    return Text(f"{s:5.1f}", style=_resolve(app, style), justify="right")


@dataclass
class Session:
    source: str | None = None
    target: str | None = None
    playlists: list[Playlist] = field(default_factory=list)
    dry_run: bool = False
    prefix: str = ""
    preset_name: str = ""


class StepBar(Static):
    """Top bar: wordmark and the five steps, current one highlighted."""

    def __init__(self, current: int, subtitle: str = "") -> None:
        super().__init__(classes="stepbar")
        self.current = current
        self.subtitle = subtitle

    def render(self) -> Table:
        left = Text()
        left.append(" tunebridge ", style=_resolve(self.app, "bold $background on $primary"))
        left.append("  ")
        for i, name in enumerate(STEPS):
            left.append("  ")
            if i == self.current:
                left.append(f" {i + 1} {name} ", style=_resolve(self.app, "bold $foreground on $surface"))
            elif i < self.current:
                left.append(f" {i + 1} {name} ", style=_resolve(self.app, "$success"))
            else:
                left.append(f" {i + 1} {name} ", style="dim")
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(left, Text(self.subtitle + " ", style="dim italic"))
        return grid


HOW_IT_WORKS = (
    "[b]1[/b]  Pick a platform on the left and press [b]l[/b] to log in. Close the window when you are done.\n"
    "[b]2[/b]  Choose where to move from and to, then [b]Continue[/b].\n"
    "[b]3[/b]  Tick the playlists you want. Every song is looked up on the other platform.\n"
    "[b]4[/b]  Check the uncertain matches, then [b]Transfer[/b]. A CSV report is saved at the end.\n"
    "[dim]Run it again later: songs that were already moved are skipped, only new ones are added.[/dim]"
)

# ---------------------------------------------------------------------------------------------
# 1. Connect
# ---------------------------------------------------------------------------------------------


class ConnectScreen(Screen):
    BINDINGS = [
        Binding("l", "login", "Log in"),
        Binding("c", "check", "Check session"),
        Binding("x", "logout", "Forget session"),
        Binding("enter", "next", "Continue", show=True, priority=False),
        Binding("ctrl+t", "app.toggle_dark_mode", "Theme"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield StepBar(0)
        with Horizontal(id="connect"):
            with Vertical(id="platforms-pane", classes="pane"):
                yield Label("Platforms", classes="pane-title")
                yield Static(
                    "Log in once per platform. A normal browser window opens; you sign in yourself, "
                    "then close it. The session stays on this computer only.",
                    classes="hint",
                )
                yield DataTable(id="platforms", cursor_type="row", zebra_stripes=False)
                yield Static("", id="connect-status", classes="status-line")
                yield Label("How it works", classes="pane-title")
                yield Static(HOW_IT_WORKS, id="how", classes="how")
            with Vertical(id="route-pane", classes="pane"):
                yield Label("Route", classes="pane-title")
                yield Label("Move from", classes="field-label")
                yield OptionList(id="source")
                yield Label("Move to", classes="field-label")
                yield OptionList(id="target")
                yield Static("", id="route-summary", classes="route-summary")
                yield Button("Continue", id="continue", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#platforms", DataTable)
        table.add_column("Platform", key="platform", width=20)
        table.add_column("Session", key="session", width=16)
        table.add_column("Browser", key="mode", width=12)
        table.add_column("Notes", key="notes", width=58)
        self.refresh_platforms()
        src = self.query_one("#source", OptionList)
        dst = self.query_one("#target", OptionList)
        for name, cls in sorted(registry().items(), key=lambda kv: kv[1].label):
            src.add_option(Option(cls.label, id=name))
            dst.add_option(Option(cls.label, id=name))
        table.focus()

    def refresh_platforms(self, checked: dict[str, bool] | None = None) -> None:
        checked = checked or {}
        table = self.query_one("#platforms", DataTable)
        cursor = table.cursor_row
        table.clear()
        for name, cls in sorted(registry().items(), key=lambda kv: kv[1].label):
            if not cls.uses_browser:
                session = Text("not needed", style="dim")
                mode = Text("-", style="dim")
            else:
                if name in checked:
                    session = Text("logged in", style=_resolve(self.app, "$success")) if checked[name] \
                        else Text("logged out", style=_resolve(self.app, "$error"))
                elif has_profile(name):
                    session = Text("saved", style=_resolve(self.app, "$success"))
                else:
                    session = Text("not logged in", style=_resolve(self.app, "$warning"))
                mode = Text("background" if cls.headless_ok else "window")
            notes = Text()
            if cls.experimental:
                notes.append("experimental  ", style=_resolve(self.app, "$warning"))
            notes.append(cls.note, style="dim")
            table.add_row(Text(cls.label, style="bold"), session, mode, notes, key=name)
        if cursor is not None and table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))

    def on_theme_switched(self) -> None:
        self.refresh_platforms()

    def _selected_platform(self) -> str | None:
        table = self.query_one("#platforms", DataTable)
        if not table.row_count:
            return None
        row_key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
        return str(row_key.value)

    def _status(self, msg: str) -> None:
        self.query_one("#connect-status", Static).update(msg)

    def action_login(self) -> None:
        name = self._selected_platform()
        if not name:
            return
        cls = registry()[name]
        if not cls.uses_browser:
            self._status(f"{cls.label} does not need a login.")
            return
        self.run_login(name)

    @work(thread=True, exclusive=True, group="session")
    def run_login(self, name: str) -> None:
        cls = registry()[name]
        say = lambda m: self.app.call_from_thread(self._status, m)  # noqa: E731
        try:
            proc = launch_login_window(name, cls.login_url or cls.home_url)
        except BrowserError as e:
            say(f"[b]Error:[/b] {e}")
            return
        say(f"A browser window is open for [b]{cls.label}[/b]. Log in, then close the window.")
        proc.wait()
        say(f"Checking the {cls.label} session...")
        ok = self._check(name)
        self.app.call_from_thread(self.refresh_platforms, {name: ok})
        say(f"Logged in to [b]{cls.label}[/b]." if ok else f"Could not confirm the {cls.label} login. Try again.")

    def _check(self, name: str) -> bool:
        try:
            with get_provider(name) as p:
                return p.is_logged_in()
        except BrowserError as e:
            self.app.call_from_thread(self._status, f"[b]Error:[/b] {e}")
            return False

    def action_check(self) -> None:
        name = self._selected_platform()
        if name:
            self.run_check(name)

    @work(thread=True, exclusive=True, group="session")
    def run_check(self, name: str) -> None:
        cls = registry()[name]
        self.app.call_from_thread(self._status, f"Opening {cls.label} to check the session...")
        ok = self._check(name)
        self.app.call_from_thread(self.refresh_platforms, {name: ok})
        self.app.call_from_thread(self._status, f"{cls.label}: {'logged in' if ok else 'not logged in'}.")

    def action_logout(self) -> None:
        name = self._selected_platform()
        if name and registry()[name].uses_browser:
            removed = remove_profile(name)
            self.refresh_platforms()
            self._status(f"Forgot the {registry()[name].label} session." if removed else "Nothing to forget.")

    @on(OptionList.OptionHighlighted)
    @on(OptionList.OptionSelected)
    def route_changed(self) -> None:
        src = self.query_one("#source", OptionList)
        dst = self.query_one("#target", OptionList)
        s = src.get_option_at_index(src.highlighted).id if src.highlighted is not None else None
        d = dst.get_option_at_index(dst.highlighted).id if dst.highlighted is not None else None
        session: Session = self.app.session  # type: ignore[attr-defined]
        session.source, session.target = s, d
        summary = self.query_one("#route-summary", Static)
        btn = self.query_one("#continue", Button)
        reg = registry()
        if s and d and s == d:
            summary.update(Text("Pick two different platforms.", style=_resolve(self.app, "$warning")))
            btn.disabled = True
        elif s and d:
            summary.update(Text.assemble((reg[s].label, "bold"), ("  to  ", "dim"), (reg[d].label, "bold")))
            btn.disabled = False
        else:
            summary.update(Text("Choose where to move from and to.", style="dim"))
            btn.disabled = True

    @on(Button.Pressed, "#continue")
    def action_next(self) -> None:
        if self.query_one("#continue", Button).disabled:
            return
        self.app.push_screen(PlaylistsScreen())


# ---------------------------------------------------------------------------------------------
# 2. Playlists
# ---------------------------------------------------------------------------------------------


class PlaylistsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+a", "select_all", "Select all"),
        Binding("ctrl+n", "select_none", "Select none"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("ctrl+f", "focus_filter", "Filter"),
        Binding("ctrl+t", "app.toggle_dark_mode", "Theme"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.all: list[Playlist] = []
        self.chosen: set[str] = set()

    def compose(self) -> ComposeResult:
        s: Session = self.app.session  # type: ignore[attr-defined]
        reg = registry()
        yield StepBar(1, f"{reg[s.source].label} to {reg[s.target].label}")
        with Horizontal(id="playlists-layout"):
            with Vertical(id="playlists-pane", classes="pane"):
                yield Label(f"Your playlists on {reg[s.source].label}", classes="pane-title")
                yield Input(placeholder="Filter playlists", id="filter")
                with Vertical(id="pl-loading"):
                    yield LoadingIndicator()
                    yield Static("", id="pl-loading-msg", classes="hint center")
                yield SelectionList[str](id="pl-list")
                yield Static("", id="pl-count", classes="status-line")
            with Vertical(id="options-pane", classes="pane"):
                yield Label("Options", classes="pane-title")
                yield Label("Name prefix on the target", classes="field-label")
                yield Input(placeholder="e.g. 'from Spotify - '", id="prefix")
                with Horizontal(classes="switch-row"):
                    yield Switch(id="dry-run", value=False)
                    yield Label("Dry run (match only, write nothing)")
                yield Label("Save this selection as a preset", classes="field-label")
                yield Input(placeholder="Preset name (optional)", id="preset")
                yield Static(
                    "Presets let you run the same move again later with "
                    "[b]tunebridge sync <name>[/b]. Only new songs are added.",
                    classes="hint",
                )
                yield Button("Find matches", id="go", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pl-list").display = False
        self.load()

    def action_reload(self) -> None:
        self.query_one("#pl-list").display = False
        self.query_one("#pl-loading").display = True
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        s: Session = self.app.session  # type: ignore[attr-defined]
        reg = registry()
        msg = lambda m: self.app.call_from_thread(self.query_one("#pl-loading-msg", Static).update, m)  # noqa: E731
        msg(f"Opening {reg[s.source].label}"
            + ("" if reg[s.source].headless_ok else " in a browser window") + "...")
        try:
            with get_provider(s.source) as p:
                if p.uses_browser and not p.is_logged_in():
                    msg(f"Not logged in to {reg[s.source].label}. Go back (Esc) and log in first.")
                    return
                msg("Reading your library...")
                pls = p.list_playlists()
        except BrowserError as e:
            msg(f"Error: {e}")
            return
        self.app.call_from_thread(self.show, pls)

    def show(self, pls: list[Playlist]) -> None:
        self.all = pls
        self.query_one("#pl-loading").display = False
        lst = self.query_one("#pl-list", SelectionList)
        lst.display = True
        if not pls:
            self.query_one("#pl-count", Static).update("No playlists found.")
            return
        self.fill()
        lst.focus()

    def fill(self) -> None:
        q = self.query_one("#filter", Input).value.strip().lower()
        lst = self.query_one("#pl-list", SelectionList)
        lst.clear_options()
        width = max((len(p.name) for p in self.all), default=10)
        width = min(max(width, 16), 48)
        for p in self.all:
            if q and q not in p.name.lower():
                continue
            label = Text()
            name = p.name if len(p.name) <= width else p.name[: width - 1] + "~"
            label.append(f"{name:<{width}}  ")
            label.append(f"{p.track_count if p.track_count is not None else '?':>5} tracks", style="dim")
            if p.owner:
                label.append(f"   {p.owner}", style="dim italic")
            if p.kind == "liked":
                label.append("   liked", style=_resolve(self.app, "$secondary"))
            lst.add_option((label, p.id, p.id in self.chosen))
        self.update_count()

    @on(Input.Changed, "#filter")
    def filter_changed(self) -> None:
        self.fill()

    @on(SelectionList.SelectedChanged)
    def selection_changed(self) -> None:
        lst = self.query_one("#pl-list", SelectionList)
        visible = {lst.get_option_at_index(i).value for i in range(lst.option_count)}
        self.chosen = (self.chosen - visible) | set(lst.selected)
        self.update_count()

    def update_count(self) -> None:
        picked = [p for p in self.all if p.id in self.chosen]
        tracks = sum(p.track_count or 0 for p in picked)
        self.query_one("#pl-count", Static).update(
            f"{len(picked)} of {len(self.all)} selected" + (f"  /  about {tracks} tracks" if tracks else "")
        )
        self.query_one("#go", Button).disabled = not picked

    def action_select_all(self) -> None:
        self.query_one("#pl-list", SelectionList).select_all()

    def action_select_none(self) -> None:
        self.query_one("#pl-list", SelectionList).deselect_all()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    @on(Button.Pressed, "#go")
    def go(self) -> None:
        s: Session = self.app.session  # type: ignore[attr-defined]
        s.playlists = [p for p in self.all if p.id in self.chosen]
        s.prefix = self.query_one("#prefix", Input).value
        s.dry_run = self.query_one("#dry-run", Switch).value
        s.preset_name = self.query_one("#preset", Input).value.strip()
        if s.preset_name:
            save_preset(Preset(name=s.preset_name, source=s.source or "", target=s.target or "",
                               playlists=[p.name for p in s.playlists], prefix=s.prefix))
            self.notify(f"Saved preset '{s.preset_name}'.")
        self.app.push_screen(RunScreen())


# ---------------------------------------------------------------------------------------------
# 3-5. Review, transfer, report
# ---------------------------------------------------------------------------------------------


class CandidateScreen(ModalScreen[tuple[Track | None, float] | None]):
    """Pick a different match for one song, or skip it."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, result: TrackResult) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        src = self.result.source
        with Vertical(id="cand-box", classes="pane"):
            yield Label("Choose the right song", classes="pane-title")
            yield Static(Text.assemble(("Looking for  ", "dim"), (src.title, "bold"), ("  ", ""),
                                       (src.artist_line, ""), ("  " + fmt_duration(src.duration), "dim")))
            opts = OptionList(id="cand-list")
            yield opts
            yield Static("Enter to choose, Esc to cancel.", classes="hint")

    def on_mount(self) -> None:
        opts = self.query_one("#cand-list", OptionList)
        for i, (t, s) in enumerate(self.result.candidates):
            line = Text()
            line.append(f"{s:5.1f}  ", style="bold")
            line.append(t.title)
            line.append(f"  {t.artist_line}", style="dim")
            line.append(f"  {fmt_duration(t.duration)}", style="dim")
            opts.add_option(Option(line, id=str(i)))
        opts.add_option(Option(Text("Skip this song", style="italic"), id="skip"))
        opts.focus()

    @on(OptionList.OptionSelected)
    def chosen(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "skip":
            self.dismiss((None, 0.0))
        else:
            t, s = self.result.candidates[int(ev.option.id or 0)]
            self.dismiss((t, s))


class RunScreen(Screen):
    BINDINGS = [
        Binding("t", "transfer", "Transfer"),
        Binding("space", "toggle", "Approve / skip"),
        Binding("enter", "pick", "Choose match"),
        Binding("a", "approve_all", "Approve all"),
        Binding("1", "view('all')", "All"),
        Binding("2", "view('review')", "To check"),
        Binding("3", "view('not_found')", "Not found"),
        Binding("escape", "back", "Back / cancel"),
        Binding("ctrl+t", "app.toggle_dark_mode", "Theme"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[PlaylistJob] = []
        self.engine: TransferEngine | None = None
        self.phase = "read"
        self.view = "all"
        self.rows: dict[str, tuple[int, int]] = {}  # row key -> (job index, result index)
        self.bars: dict[int, ProgressBar] = {}
        self.report: str | None = None

    def compose(self) -> ComposeResult:
        s: Session = self.app.session  # type: ignore[attr-defined]
        reg = registry()
        yield StepBar(2, f"{reg[s.source].label} to {reg[s.target].label}" + ("  (dry run)" if s.dry_run else ""))
        with Horizontal(id="run-layout"):
            with Vertical(id="run-main", classes="pane"):
                with Horizontal(id="run-head"):
                    yield Label("Matches", classes="pane-title", id="run-title")
                    yield Static("", id="view-tabs")
                yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
                yield Static("", id="totals", classes="status-line")
            with Vertical(id="run-side", classes="pane"):
                yield Label("Progress", classes="pane-title")
                yield Static("", id="phase", classes="phase")
                yield VerticalScroll(id="bars")
                yield Label("Log", classes="pane-title")
                yield RichLog(id="log", wrap=True, markup=False, max_lines=500)
                yield Button("Transfer", id="transfer", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#results", DataTable)
        t.add_column("", key="status", width=6)
        t.add_column("Score", key="score", width=5)
        t.add_column("Playlist", key="playlist", width=16)
        t.add_column("Song", key="song", width=40)
        t.add_column("Match on target", key="match", width=40)
        self.render_tabs()
        s: Session = self.app.session  # type: ignore[attr-defined]
        for i, p in enumerate(s.playlists):
            self.add_bar(i, p.name)
        self.prepare()

    # ---- progress bars ---------------------------------------------------------------

    def add_bar(self, i: int, name: str) -> None:
        box = self.query_one("#bars", VerticalScroll)
        row = Vertical(classes="bar-row")
        box.mount(row)
        row.mount(Label(name, classes="bar-label"))
        bar = ProgressBar(total=None, show_eta=False, id=f"bar{i}")
        row.mount(bar)
        self.bars[i] = bar

    # ---- engine plumbing ---------------------------------------------------------

    def _emit(self, e: Event) -> None:
        self.app.call_from_thread(self.on_engine_event, e)

    def on_engine_event(self, e: Event) -> None:
        log = self.query_one("#log", RichLog)
        d = e.data
        if e.kind == "phase":
            self.query_one("#phase", Static).update(Text(e.message, style="bold"))
            log.write(Text(e.message, style="bold"))
            ph = d.get("phase")
            if ph in ("read", "match"):
                self.phase = ph
                for b in self.bars.values():
                    b.update(total=None, progress=0)
        elif e.kind == "progress":
            bar = self.bars.get(d.get("index", -1))
            if bar is not None:
                bar.update(total=d.get("total") or None, progress=d.get("done", 0))
        elif e.kind == "track":
            self.add_result(d["index"], d["pos"])
        elif e.kind in ("log", "warn", "error"):
            style = {"warn": "$warning", "error": "$error"}.get(e.kind, "")
            log.write(Text(e.message, style=_resolve(self.app, style) if style else "dim"))
        elif e.kind == "done":
            self.report = d.get("report")

    @work(thread=True, exclusive=True, group="engine")
    def prepare(self) -> None:
        s: Session = self.app.session  # type: ignore[attr-defined]
        try:
            self.engine = TransferEngine(s.source or "", s.target or "", emit=self._emit, dry_run=s.dry_run, prefix=s.prefix)
            jobs = self.engine.prepare(s.playlists)
        except Cancelled:
            self._emit(Event("warn", "Cancelled."))
            return
        except Exception as e:  # show it, do not crash the UI
            self._emit(Event("error", f"{e}"))
            return
        self.app.call_from_thread(self.prepared, jobs)

    def prepared(self, jobs: list[PlaylistJob]) -> None:
        self.jobs = jobs
        self.phase = "review"
        self.refresh_table()
        btn = self.query_one("#transfer", Button)
        n = self.approved_count()
        btn.label = f"Transfer {n} songs" if not self.app.session.dry_run else f"Dry run {n} songs"  # type: ignore[attr-defined]
        btn.disabled = n == 0
        if any(r.status == "review" for j in jobs for r in j.results):
            self.notify("Some matches need a look. Press 2 to see them, Space to approve, Enter to pick another.",
                        timeout=8)
        self.query_one("#results", DataTable).focus()

    @work(thread=True, exclusive=True, group="engine")
    def run_apply(self) -> None:
        assert self.engine is not None
        try:
            self.engine.apply(self.jobs)
        except Cancelled:
            self._emit(Event("warn", "Cancelled. Songs added so far are remembered."))
        except Exception as e:
            self._emit(Event("error", f"{e}"))
        finally:
            self.engine.close()
        self.app.call_from_thread(self.finished)

    def finished(self) -> None:
        self.phase = "done"
        self.query_one(StepBar).current = 4
        self.query_one(StepBar).refresh()
        self.refresh_table()
        summary = TransferEngine.summary(self.jobs)
        dry = self.app.session.dry_run  # type: ignore[attr-defined]
        added = summary.get("would_add" if dry else "added", 0)
        text = Text()
        text.append("Done.  ", style="bold")
        text.append(f"{added} {'would be added' if dry else 'added'}", style=_resolve(self.app, "$success"))
        text.append("\n")
        text.append(f"{summary.get('already', 0)} already there")
        text.append("   ")
        text.append(f"{summary.get('not_found', 0) + summary.get('review', 0)} not moved",
                    style=_resolve(self.app, "$warning"))
        text.append("   ")
        text.append(f"{summary.get('failed', 0)} failed", style=_resolve(self.app, "$error") if summary.get("failed") else "dim")
        self.query_one("#phase", Static).update(text)
        if self.report:
            self.query_one("#log", RichLog).write(Text(f"Report saved: {self.report}", style="bold"))
        btn = self.query_one("#transfer", Button)
        btn.label = "Start over"
        btn.disabled = False

    # ---- table -------------------------------------------------------------------

    def _visible(self, r: TrackResult) -> bool:
        if self.view == "review":
            return r.status == "review"
        if self.view == "not_found":
            return r.status in ("not_found", "failed")
        return True

    def _cells(self, ji: int, r: TrackResult) -> tuple:
        src = r.source
        song = Text(src.title, overflow="ellipsis", no_wrap=True)
        if src.artists:
            song.append(f"  {src.artist_line}", style="dim")
        if r.match is not None and r.status not in ("already",):
            m = Text(r.match.title, overflow="ellipsis", no_wrap=True)
            if r.match.artists:
                m.append(f"  {r.match.artist_line}", style="dim")
        elif r.status == "already":
            m = Text("already moved earlier", style="dim italic")
        elif r.status == "not_found":
            m = Text("no good match", style="dim italic")
        else:
            m = Text("")
        pl = Text(self.jobs[ji].source.name if self.jobs else "", style="dim", overflow="ellipsis", no_wrap=True)
        return badge(self.app, r), score_text(self.app, r), pl, song, m

    def add_result(self, ji: int, pos: int) -> None:
        """Append one row while matching is still running."""
        if self.engine is None or not self.engine.jobs:
            return
        self.jobs = self.engine.jobs
        r = self.jobs[ji].results[pos]
        if not self._visible(r):
            return
        key = f"{ji}:{pos}"
        if key in self.rows:
            return
        self.query_one("#results", DataTable).add_row(*self._cells(ji, r), key=key)
        self.rows[key] = (ji, pos)
        self.update_totals()

    def refresh_table(self) -> None:
        t = self.query_one("#results", DataTable)
        t.clear()
        self.rows.clear()
        for ji, job in enumerate(self.jobs):
            for ri, r in enumerate(job.results):
                if not self._visible(r):
                    continue
                key = f"{ji}:{ri}"
                t.add_row(*self._cells(ji, r), key=key)
                self.rows[key] = (ji, ri)
        self.update_totals()
        self.render_tabs()

    def update_row(self, key: str) -> None:
        ji, ri = self.rows[key]
        r = self.jobs[ji].results[ri]
        t = self.query_one("#results", DataTable)
        for col, val in zip(("status", "score", "playlist", "song", "match"), self._cells(ji, r)):
            t.update_cell(key, col, val)
        self.update_totals()

    def approved_count(self) -> int:
        return sum(1 for j in self.jobs for r in j.results if r.approved and r.status in ("matched", "review"))

    def update_totals(self) -> None:
        c: dict[str, int] = {}
        for j in self.jobs:
            for r in j.results:
                c[r.status] = c.get(r.status, 0) + 1
        appr = sum(1 for j in self.jobs for r in j.results if r.status == "review" and r.approved)
        text = Text()
        text.append(f"{c.get('matched', 0) + c.get('added', 0)} matched", style=_resolve(self.app, "$success"))
        text.append("   ")
        text.append(f"{c.get('review', 0)} to check ({appr} approved)", style=_resolve(self.app, "$warning"))
        text.append("   ")
        text.append(f"{c.get('not_found', 0)} not found", style=_resolve(self.app, "$error"))
        text.append("   ")
        text.append(f"{c.get('already', 0)} already moved", style="dim")
        self.query_one("#totals", Static).update(text)
        if self.phase == "review":
            n = self.approved_count()
            btn = self.query_one("#transfer", Button)
            btn.label = f"Transfer {n} songs" if not self.app.session.dry_run else f"Dry run {n} songs"  # type: ignore[attr-defined]
            btn.disabled = n == 0

    def render_tabs(self) -> None:
        def tab(key: str, num: str, name: str) -> Text:
            style = "bold underline" if self.view == key else "dim"
            return Text(f"{num} {name}", style=style)

        self.query_one("#view-tabs", Static).update(
            Text("   ").join([tab("all", "1", "All"), tab("review", "2", "To check"), tab("not_found", "3", "Not found")])
        )

    def _current(self) -> tuple[str, TrackResult] | None:
        t = self.query_one("#results", DataTable)
        if not t.row_count or self.phase != "review":
            return None
        key = str(t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value)
        ji, ri = self.rows[key]
        return key, self.jobs[ji].results[ri]

    # ---- actions -----------------------------------------------------------------

    def on_theme_switched(self) -> None:
        if self.jobs and self.phase not in ("read", "match"):
            self.refresh_table()

    def action_view(self, view: str) -> None:
        self.view = view
        if self.jobs:
            self.refresh_table()
        else:
            self.render_tabs()

    def action_toggle(self) -> None:
        cur = self._current()
        if not cur:
            return
        key, r = cur
        if r.status in ("matched", "review") and r.match is not None:
            r.approved = not r.approved
            self.update_row(key)

    def action_approve_all(self) -> None:
        if self.phase != "review":
            return
        for j in self.jobs:
            for r in j.results:
                if r.status == "review" and r.match is not None:
                    r.approved = True
        self.refresh_table()

    def action_pick(self) -> None:
        cur = self._current()
        if not cur:
            return
        key, r = cur
        if not r.candidates:
            self.notify("No other candidates for this song.")
            return

        def done(res: tuple[Track | None, float] | None) -> None:
            if res is None:
                return
            track, s = res
            if track is None:
                r.approved = False
                if r.status == "matched":
                    r.status = "review"
            else:
                r.match, r.score, r.approved = track, s, True
                r.status = "matched" if s >= 80 else "review"
            self.update_row(key)

        self.app.push_screen(CandidateScreen(r), done)

    @on(DataTable.RowSelected, "#results")
    def row_selected(self) -> None:
        self.action_pick()

    @on(Button.Pressed, "#transfer")
    def action_transfer(self) -> None:
        if self.phase == "done":
            self.app.pop_screen()
            self.app.pop_screen()
            return
        if self.phase != "review" or self.approved_count() == 0:
            return
        self.phase = "write"
        self.query_one(StepBar).current = 3
        self.query_one(StepBar).refresh()
        btn = self.query_one("#transfer", Button)
        btn.disabled = True
        btn.label = "Transferring..."
        for b in self.bars.values():
            b.update(total=None, progress=0)
        self.run_apply()

    def action_back(self) -> None:
        if self.phase in ("read", "match", "write") and self.engine is not None:
            self.engine.cancel()
            self.notify("Cancelling after the current song...")
            return
        if self.engine is not None and self.phase != "write":
            self._close_engine()
        self.app.pop_screen()

    @work(thread=True)
    def _close_engine(self) -> None:
        if self.engine is not None:
            self.engine.close()

    def action_quit_app(self) -> None:
        if self.engine is not None:
            self.engine.cancel()
            self._close_engine()
        self.app.exit()


class TuneBridgeApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "tunebridge"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self.session = Session()

    def on_mount(self) -> None:
        self.register_theme(DARK)
        self.register_theme(LIGHT)
        self.theme = "tunebridge-dark"
        self.push_screen(ConnectScreen())

    def action_toggle_dark_mode(self) -> None:
        self.theme = "tunebridge-light" if self.theme == "tunebridge-dark" else "tunebridge-dark"
        for screen in self.screen_stack:
            hook = getattr(screen, "on_theme_switched", None)
            if hook:
                hook()
            for bar in screen.query(StepBar):
                bar.refresh()


def run_tui() -> None:
    TuneBridgeApp().run()


if __name__ == "__main__":
    run_tui()

