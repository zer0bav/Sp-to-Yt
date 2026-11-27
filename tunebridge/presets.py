"""Saved selections: "move these playlists from A to B". Re-run any time with `tunebridge sync <name>`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .paths import config_dir


@dataclass
class Preset:
    name: str
    source: str
    target: str
    playlists: list[str] = field(default_factory=list)  # playlist names (or ids) on the source
    all_playlists: bool = False
    prefix: str = ""
    include_review: bool = False
    min_score: float = 80.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _path():
    return config_dir() / "presets.json"


def load_presets() -> dict[str, Preset]:
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out = {}
    for name, d in raw.items():
        d = {k: v for k, v in d.items() if k in Preset.__dataclass_fields__}
        d["name"] = name
        out[name] = Preset(**d)
    return out


def save_preset(preset: Preset) -> None:
    presets = load_presets()
    presets[preset.name] = preset
    _write(presets)


def delete_preset(name: str) -> bool:
    presets = load_presets()
    if name not in presets:
        return False
    del presets[name]
    _write(presets)
    return True


def _write(presets: dict[str, Preset]) -> None:
    data = {n: {k: v for k, v in p.to_dict().items() if k != "name"} for n, p in presets.items()}
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
