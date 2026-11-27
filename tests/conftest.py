import os

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own data dir and the offline demo platforms."""
    monkeypatch.setenv("TUNEBRIDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TUNEBRIDGE_EXPORTS", str(tmp_path / "exports"))
    monkeypatch.setenv("TUNEBRIDGE_DEMO", "1")
    monkeypatch.setenv("TUNEBRIDGE_DEMO_DELAY", "0")
    from tunebridge.providers import _load_all  # noqa: F401
    from tunebridge.providers import demo  # noqa: F401  (registers demo platforms)

    yield tmp_path
    os.environ.pop("TUNEBRIDGE_DEMO", None)
