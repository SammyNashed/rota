"""Config load/save. TOML on disk, plain dicts in memory.

Defaults mirror Rovyl's DEFAULT_UI_CONFIG so the wheel feels identical:
radius 140, icon 64, dead zone 60, angle targeting.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "rota"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULTS = {
    "general": {
        # A press shorter than this is a real middle click and is passed through
        # to whatever is under the cursor, so middle-click paste keeps working.
        "hold_ms": 180,
        # hold  — hold to open, release to launch (the gesture).
        # click — a click opens the wheel and leaves it up; a second click launches.
        "trigger_mode": "hold",
        "selection_mode": "angle",      # angle | cursor
        "trigger_button": "middle",     # middle | side | extra | none
        # Only react when the cursor is over bare desktop. Over a window the
        # button is passed straight to the app, held presses included.
        "desktop_only": True,
        "active_workspace": 0,
        "grab_mouse": True,
    },
    "appearance": {
        "menu_radius": 140,
        "icon_size": 64,
        "activation_threshold": 60,
        "app_spacing": 10,
        "hover_color": "#FFFFFF",
        "backdrop_opacity": 0.0,
        "menu_opacity": 0.8,
        "show_labels": True,
        # False keeps every label visible; True shows only the aimed-at one.
        "labels_on_hover_only": False,
        "background_style": "circle",   # circle | fullscreen
        # How far past the ring angle-targeting still counts, in pixels.
        # Beyond this nothing is highlighted and releasing cancels, so moving
        # the cursor away is a way to change your mind. 0 disables the limit
        # (upstream's behaviour: a slice stays aimed from anywhere on screen).
        "aim_reach": 110,
    },
    "hud": {
        "show_clock": False,
        "clock_24h": True,
        "clock_position": "top-center",  # top-left | top-center | top-right
                                         # bottom-left | bottom-center | bottom-right
        "show_battery": False,
        "show_weather": False,
        "weather_location": "",
    },
    "center": {
        # picker — the hub opens the workspace wheel (upstream's behaviour).
        # none   — the hub only cancels.
        # launch — the hub runs a target of its own.
        "action": "picker",
        "target": "",
        "label": "",
        "icon": "view-grid-symbolic",
    },
    "game_mode": {
        "enabled": False,
        # Suppress the wheel while anything is fullscreen.
        "auto_detect_fullscreen": True,
        # ...and/or while one of these window classes is focused.
        "blocked_classes": [],
    },
}

SECTIONS = ("general", "appearance", "hud", "center", "game_mode")


def _default_workspaces():
    return [
        {"name": "MAIN", "slices": []},
        {"name": "CODE", "slices": []},
        {"name": "MEDIA", "slices": []},
    ]


def load() -> dict:
    cfg = {name: dict(DEFAULTS[name]) for name in SECTIONS}
    cfg["workspaces"] = _default_workspaces()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "rb") as fh:
                raw = tomllib.load(fh)
        except Exception as exc:  # a corrupt file must not cost you the launcher
            print(f"[rota] config unreadable ({exc}); using defaults")
            return cfg
        # Spread the stored values OVER the defaults, never the other way round:
        # a setting added after this file was written must arrive as its default,
        # not as None.
        for section in SECTIONS:
            cfg[section].update(raw.get(section) or {})
        if raw.get("workspaces"):
            cfg["workspaces"] = raw["workspaces"]
    return cfg


def _emit_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_emit_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_emit_value(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(cfg: dict) -> None:
    """Minimal emitter for our own fixed schema — stdlib has no TOML writer."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for section in SECTIONS:
        lines.append(f"[{section}]")
        for key, value in cfg[section].items():
            lines.append(f"{key} = {_emit_value(value)}")
        lines.append("")
    for workspace in cfg["workspaces"]:
        lines.append("[[workspaces]]")
        lines.append(f'name = {_emit_value(workspace.get("name", "?"))}')
        slices = workspace.get("slices") or []
        if slices:
            lines.append("slices = [")
            for entry in slices:
                lines.append(f"  {_emit_value(entry)},")
            lines.append("]")
        else:
            lines.append("slices = []")
        lines.append("")
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, CONFIG_PATH)   # atomic: a half-written config is a broken launcher
