# Contributing

Thanks for helping. The most valuable contributions are usually small: a selector that changed on a
website, or a new platform.

## Setup

```bash
uv venv && uv pip install -e '.[dev]'
pytest
TUNEBRIDGE_DEMO=1 tunebridge          # offline demo platforms for UI work
```

## Fixing a broken platform

Websites change their markup. Each platform keeps its selectors in
`tunebridge/providers/selectors/<platform>.toml`.

1. Run the failing command with `--show-browser` and watch where it stops.
2. Open the browser's DevTools on that page and find a stable hook: prefer `data-testid`,
   `data-test`, `aria-label` or `role` attributes over generated class names or long XPaths.
3. Update the TOML file, run the command again, and send a pull request that says which page and
   which step you fixed.

## Adding a platform

1. Create `tunebridge/providers/<name>.py` with a class that subclasses `Provider` and is decorated
   with `@register`. Implement:

   | method | returns |
   |---|---|
   | `is_logged_in()` | `bool` |
   | `list_playlists()` | `list[Playlist]` (set `editable=False` for things you cannot add to) |
   | `get_tracks(playlist, progress)` | `list[Track]` |
   | `search(track, limit)` | `list[Track]` with an `id` that `add_tracks` understands |
   | `create_playlist(name, description)` | `Playlist` |
   | `add_tracks(playlist, tracks, progress)` | the subset of `tracks` (same objects) that was added |

2. Put selectors in `tunebridge/providers/selectors/<name>.toml`; read them through `self.sel`.
3. Add the module name to `MODULES` in `tunebridge/providers/_load_all.py`.
4. Use the helpers on `self.browser` (`wait_css`, `wait_any`, `click_text`, `js`) and
   `self.collect_scrolling(...)` for long, virtualised lists.
5. Set `headless_ok = False` if the site refuses headless browsers, and `experimental = True` until
   create/add has been verified against a real account.

## Rules

- Never ask for or store passwords. Logins happen in a plain browser window the user controls.
- Do not try to hide automation or bypass bot detection. If a site needs a visible window, use one.
- Keep it gentle: one page at a time, no parallel hammering of a site.
- All user-facing text is in English.
- Run `pytest` before sending a pull request.
