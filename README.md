# TuneBridge

Move your playlists between **Spotify, YouTube Music, Deezer, Apple Music, TIDAL** and **CSV/JSON files**.
Pick the playlists you want, TuneBridge finds every song on the other platform, lets you check the
uncertain ones, and builds the playlists for you. Run it again later and only the new songs are added.

- **No API keys, no developer accounts, no payment.** TuneBridge drives a real Chromium browser with
  Selenium, using your own login on the normal website.
- **Your data stays on your computer.** You type your password into the real site yourself; the session
  is kept in a local browser profile. There is no server, no telemetry, nothing is uploaded.
- **Terminal UI, web UI and CLI.** Same engine underneath; use whichever you like.

```
 tunebridge    1 Connect   2 Playlists   3 Review   4 Transfer   5 Report       Spotify to YouTube Music
```

## Platform support

| Platform        | Read playlists | Search | Create + add | Browser mode | Notes |
|-----------------|:-:|:-:|:-:|---|---|
| Spotify         | yes | yes | yes | background (headless) | Free and Premium |
| YouTube Music   | yes | yes | yes | visible window | YouTube Music refuses headless browsers |
| Deezer          | yes | yes | experimental | background | |
| Apple Music     | yes | yes | experimental | background | needs an Apple Music subscription |
| TIDAL           | experimental | experimental | experimental | visible window | |
| CSV / JSON file | yes | - | yes | none | backup, or a bridge to anything else |

"Experimental" means the code follows the site's menus but has not been verified against a real
account yet. If something breaks, the fix is usually a one-line selector change, see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Install

You need Python 3.11+ and any Chromium-based browser (Chromium, Chrome, Brave, Edge, Vivaldi).
If none is installed, Selenium downloads a private copy of Chrome for Testing on first run.

```bash
git clone https://github.com/zer0bav/Sp-to-Yt
cd Sp-to-Yt
pip install .            # or: uv tool install .   /   pipx install .
```

To use a specific browser: `export TUNEBRIDGE_BROWSER=/path/to/browser`.

## Quick start

```bash
tunebridge login spotify     # a browser window opens: log in, then close it
tunebridge login ytmusic
tunebridge                   # opens the terminal UI
```

or the web UI:

```bash
tunebridge web               # http://127.0.0.1:8765
```

### The five steps (TUI and web)

1. **Connect**: see which platforms you are logged in to, log in or out, choose *from* and *to*.
2. **Playlists**: tick the playlists to move. Filter, select all, set a name prefix, dry run,
   and optionally save the selection as a **preset**.
3. **Review**: every song is searched on the target and scored 0-100.
   - `MATCH` (80+) is added automatically.
   - `CHECK` (60-80) waits for you: approve it, or pick another candidate.
   - `NONE` had no good result; you can still pick a candidate by hand.
   - `KEPT` / *Already moved* songs were added in an earlier run and are skipped.
4. **Transfer**: playlists are created (or reused) on the target and songs are added, with live progress.
5. **Report**: what was added, skipped, not found. A CSV report is saved for every run.

TUI keys: `l` log in, `c` check session, `Space` approve/skip, `Enter` choose another match,
`1/2/3` switch views, `t` transfer, `Ctrl+T` light/dark theme, `Esc` back/cancel.

### Command line

```bash
tunebridge platforms                                   # supported platforms and session status
tunebridge playlists spotify                           # list your playlists
tunebridge transfer -f spotify -t ytmusic              # pick playlists interactively
tunebridge transfer -f spotify -t ytmusic -p "Gym" -p "Chill" --dry-run
tunebridge transfer -f spotify -t deezer --all --prefix "from Spotify - "
tunebridge transfer -f spotify -t ytmusic -p "Gym" --save-preset gym
tunebridge sync gym                                    # later: adds only the new songs
tunebridge export spotify --all --format csv           # backup to ~/Music/tunebridge
tunebridge import ~/Music/tunebridge/Gym.csv --to applemusic
tunebridge logout spotify                              # delete the saved session
```

Useful flags: `--include-review` also adds the 60-80 matches without asking, `--min-score` changes the
auto-add threshold, `--show-browser` shows the browser windows while working.

### Presets and sync

A preset remembers *source, target and which playlists*. `tunebridge sync <name>` (or **Run** in the
web UI) repeats the move; TuneBridge remembers every song it has moved, so only new ones are added and
nothing is duplicated. Put it in cron or a systemd timer to keep two platforms in sync.

## How it works

```
 source site ──read──▶  engine  ──search──▶ target site
 (Selenium)            matcher               (Selenium)
                         │
                   state.db: what was moved (incremental sync)
```

- `tunebridge/providers/` has one class per platform. Each implements `list_playlists`, `get_tracks`,
  `search`, `create_playlist`, `add_tracks`.
- `tunebridge/providers/selectors/*.toml` holds the CSS selectors for each site, so a website redesign
  is usually a one-file fix.
- `tunebridge/matcher.py` normalises titles (feat., remaster, live, accents including Turkish letters)
  and scores candidates on title, artist and duration, penalising live/remix/cover versions.
- `tunebridge/engine.py` runs the two phases (match, then write) and emits progress events that the
  CLI, TUI and web UI all render.

## Privacy and security

- Logins happen in a normal browser window that TuneBridge only *launches*; it never sees your password.
- Each platform has its own profile in `~/.local/share/tunebridge/profiles/<platform>` (Linux;
  platform-specific elsewhere). `tunebridge logout <platform>` deletes it.
- The web UI listens on `127.0.0.1` only, rejects other `Host` headers (DNS rebinding), requires a
  custom header on every state-changing request (CSRF) and sends a strict Content-Security-Policy.
- No analytics, no update checks, no network traffic except to the music sites you use.

## Notes from real accounts

- **Spotify folders** in Your Library are opened one by one, so playlists inside folders are listed too.
- **Any interface language** works for YouTube Music: buttons are found through the page's own data
  (for example the "Songs" filter and the "Save to playlist" menu item), not their English text.
- **Long lists** (Liked Songs with 800+ tracks) are read completely; rows that are still loading are
  waited for instead of being skipped.
- **Cookies** are stored with your desktop keyring, the same way your normal browser does it. On a
  system without a keyring, Chromium falls back to its built-in key.

## Troubleshooting

- **"profile is in use"**: close the login window (or the other TuneBridge run) first.
- **Not logged in after closing the window**: close the login window completely (cookies are written
  when the browser exits), then run `tunebridge status <platform>`. If it still says logged out, run
  `tunebridge login <platform>` again.
- **A step fails after a site update**: run with `--show-browser` to watch it, then adjust the selector
  in `tunebridge/providers/selectors/<platform>.toml` and open a pull request.
- **Slow**: matching opens one search page per song (about 2-8 s each). Large libraries take a while,
  but runs can be cancelled and resumed; finished songs are never redone.

## Responsible use

TuneBridge automates the regular websites with your own account, at human-like speed. Automating a
website may be against its terms of service; use it for your own playlists and at your own risk.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest                                   # offline tests (uses built-in demo platforms)
TUNEBRIDGE_DEMO=1 tunebridge             # try the UI without any account
```

## License

[MIT](LICENSE)
