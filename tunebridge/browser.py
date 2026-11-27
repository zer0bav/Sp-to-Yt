"""Browser handling.

Every platform gets its own Chromium profile directory. Logging in happens in a
plain browser window that TuneBridge only launches (no WebDriver attached), so
sites see a normal browser and you type your password yourself. After that,
Selenium reuses the saved profile to read and edit playlists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import (
    SessionNotCreatedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from .paths import profile_dir


class BrowserError(RuntimeError):
    """A browser problem worth showing to the user as-is."""


_CANDIDATES: dict[str, list[str]] = {
    "linux": [
        "chromium",
        "chromium-browser",
        "google-chrome-stable",
        "google-chrome",
        "/opt/brave-bin/brave",
        "brave",
        "brave-browser",
        "/usr/bin/microsoft-edge-stable",
        "vivaldi-stable",
    ],
    "darwin": [
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
}


def find_browser() -> str | None:
    """Return a Chromium-based browser binary, or None to let Selenium Manager download one.

    Override with the TUNEBRIDGE_BROWSER environment variable.
    """
    env = os.environ.get("TUNEBRIDGE_BROWSER")
    if env:
        return env
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    for cand in _CANDIDATES.get(plat, []):
        path = cand if os.path.isabs(cand) else shutil.which(cand)
        if path and os.path.exists(path):
            return path
    return None


def _managed_browser() -> str:
    """Ask Selenium Manager for a Chrome for Testing build (downloaded once into ~/.cache/selenium)."""
    from selenium.webdriver.common.selenium_manager import SeleniumManager

    paths = SeleniumManager().binary_paths(["--browser", "chrome", "--browser-version", "stable"])
    browser = paths.get("browser_path")
    if not browser:
        raise BrowserError("No Chromium-based browser found and Selenium Manager could not download one.")
    return browser


def browser_binary() -> str:
    return find_browser() or _managed_browser()


def has_profile(platform: str) -> bool:
    p = profile_dir(platform)
    return p.exists() and any(p.iterdir())


def profile_in_use(platform: str) -> bool:
    """True when a live browser holds the profile. Stale locks from a crash or kill are removed."""
    lock = profile_dir(platform) / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return False
    try:
        target = os.readlink(lock)  # "<hostname>-<pid>"
        host, _, pid = target.rpartition("-")
        if host == os.uname().nodename and pid.isdigit():
            try:
                os.kill(int(pid), 0)
                return True
            except ProcessLookupError:
                pass
            except PermissionError:
                return True
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    (profile_dir(platform) / name).unlink()
                except FileNotFoundError:
                    pass
            return False
    except (OSError, AttributeError):
        pass
    return True


def launch_login_window(platform: str, url: str) -> subprocess.Popen:
    """Open a normal browser window (no automation) on the platform's profile.

    The user logs in by hand; when they close the window the session is kept in
    the profile directory.
    """
    profile = profile_dir(platform)
    profile.mkdir(parents=True, exist_ok=True)
    if profile_in_use(platform):
        raise BrowserError(f"The {platform} browser profile is already open. Close that window first.")
    args = [
        browser_binary(),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=en-US",
        "--new-window",
        url,
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def remove_profile(platform: str) -> bool:
    p = profile_dir(platform)
    if p.exists():
        shutil.rmtree(p)
        return True
    return False


class Browser:
    """A Selenium-driven Chromium bound to one platform profile."""

    def __init__(self, platform: str, *, headless: bool = True, window_size: str = "1400,1000"):
        self.platform = platform
        self.headless = headless
        self.window_size = window_size
        self._driver: webdriver.Chrome | None = None

    @property
    def driver(self) -> webdriver.Chrome:
        if self._driver is None:
            self._driver = self._start()
        return self._driver

    @property
    def started(self) -> bool:
        return self._driver is not None

    def _start(self) -> webdriver.Chrome:
        profile = profile_dir(self.platform)
        profile.mkdir(parents=True, exist_ok=True)
        if profile_in_use(self.platform):
            raise BrowserError(
                f"The {self.platform} browser profile is in use by another window. "
                "Close the login window (or the other TuneBridge run) and try again."
            )
        opts = Options()
        binary = find_browser()
        if binary:
            opts.binary_location = binary
        else:
            opts.browser_version = "stable"
        opts.add_argument(f"--user-data-dir={profile}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--lang=en-US")
        opts.add_argument("--mute-audio")
        opts.add_argument(f"--window-size={self.window_size}")
        # ChromeDriver normally forces a throwaway cookie key (--password-store=basic). The login
        # window uses the desktop keyring like any browser, so use the same store here; otherwise
        # the saved login cookies cannot be decrypted and the browser deletes them.
        opts.add_experimental_option("excludeSwitches", ["password-store", "use-mock-keychain"])
        if self.headless:
            opts.add_argument("--headless=new")
        try:
            drv = webdriver.Chrome(options=opts)
        except SessionNotCreatedException as e:
            raise BrowserError(f"Could not start the browser: {e.msg or e}") from e
        except WebDriverException as e:
            raise BrowserError(f"Could not start the browser: {e.msg or e}") from e
        drv.set_page_load_timeout(60)
        # Background windows never have focus, and some players (Spotify) stop loading more rows
        # of a long list until the page is focused. Tell the page it is the active window.
        try:
            drv.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except WebDriverException:
            pass
        return drv

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except WebDriverException:
                pass
            self._driver = None

    def __enter__(self) -> Browser:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- helpers -----------------------------------------------------------------

    def get(self, url: str, settle: float = 0.0) -> None:
        self.driver.get(url)
        if settle:
            time.sleep(settle)

    def js(self, script: str, *args: Any) -> Any:
        return self.driver.execute_script(script, *args)

    def wait(self, predicate: Callable[[webdriver.Chrome], Any], timeout: float = 20, message: str = "") -> Any:
        try:
            return WebDriverWait(self.driver, timeout, poll_frequency=0.25, ignored_exceptions=(StaleElementReferenceException,)).until(
                predicate, message
            )
        except TimeoutException as e:
            raise BrowserError(message or "Timed out waiting for the page.") from e

    def wait_css(self, selector: str, timeout: float = 20, visible: bool = False) -> WebElement:
        def find(d: webdriver.Chrome) -> WebElement | bool:
            for el in d.find_elements(By.CSS_SELECTOR, selector):
                if not visible or el.is_displayed():
                    return el
            return False

        return self.wait(find, timeout, f"Element not found: {selector}")

    def wait_any(self, selectors: Iterable[str], timeout: float = 20) -> tuple[str, WebElement]:
        sels = list(selectors)

        def find(d: webdriver.Chrome) -> tuple[str, WebElement] | bool:
            for s in sels:
                els = d.find_elements(By.CSS_SELECTOR, s)
                if els:
                    return s, els[0]
            return False

        return self.wait(find, timeout, f"None of these appeared: {', '.join(sels)}")

    def find(self, selector: str) -> list[WebElement]:
        return self.driver.find_elements(By.CSS_SELECTOR, selector)

    def click(self, el: WebElement) -> None:
        """Scroll into view and click; fall back to a JS click when something overlaps."""
        self.js("arguments[0].scrollIntoView({block: 'center'});", el)
        try:
            el.click()
        except WebDriverException:
            self.js("arguments[0].click();", el)

    def click_text(self, selector: str, text: str, timeout: float = 10, exact: bool = False) -> WebElement:
        """Click the first element matching `selector` whose visible text matches `text`."""
        want = text.strip().lower()

        def find(d: webdriver.Chrome) -> WebElement | bool:
            for el in d.find_elements(By.CSS_SELECTOR, selector):
                try:
                    t = (el.text or el.get_attribute("aria-label") or "").strip().lower()
                except StaleElementReferenceException:
                    continue
                if (t == want) if exact else (want in t):
                    if el.is_displayed():
                        return el
            return False

        el = self.wait(find, timeout, f"Could not find '{text}' ({selector}).")
        self.click(el)
        return el

    def screenshot(self, path: Path) -> None:
        self.driver.save_screenshot(str(path))
