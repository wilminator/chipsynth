# Copyright (C) 2026 Michael Allen Wilmes
"""The composer's remembered settings (theme, font size, scales, panel sizes, window size, last song)."""
import json
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

from .layout import Panels


def default_path():
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "chipsynth" / "composer.json"


@dataclass
class Prefs:
    dark: bool = True
    font: int = 15                   # text height in pixels; everything else scales with it
    ppb: float = 56.0                # horizontal scale: pixels per beat
    row: float = 14.0                # vertical scale: pixels per semitone
    width: int = 1280
    height: int = 760
    last: str = ""
    panels: Panels = field(default_factory=Panels)

    @classmethod
    def load(cls, path=None):
        p = Path(path) if path else default_path()
        out = cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            for f in fields(cls):
                if f.name == "panels" or f.name not in raw:
                    continue
                v = raw[f.name]
                if isinstance(v, type(getattr(out, f.name))) or (isinstance(v, int) and isinstance(getattr(out, f.name), float)):
                    setattr(out, f.name, v)
            for k, v in (raw.get("panels") or {}).items():
                if k in ("left", "right", "bottom") and isinstance(v, int):
                    setattr(out.panels, k, v)
        except (OSError, ValueError, AttributeError):
            pass
        out.font = max(11, min(28, out.font))
        out.ppb = max(6.0, min(400.0, float(out.ppb)))
        out.row = max(6.0, min(48.0, float(out.row)))
        return out

    def save(self, path=None):
        p = Path(path) if path else default_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
            return True
        except OSError:
            return False
