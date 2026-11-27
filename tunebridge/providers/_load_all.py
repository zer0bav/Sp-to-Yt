"""Import every provider so it registers itself."""

import importlib
import os

MODULES = ["spotify", "ytmusic", "deezer", "tidal", "applemusic", "file"]
if os.environ.get("TUNEBRIDGE_DEMO") == "1":
    MODULES.append("demo")

for _m in MODULES:
    try:
        importlib.import_module(f"{__package__}.{_m}")
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.{_m}":
            raise
