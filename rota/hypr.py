"""Hyprland IPC.

Only one thing is genuinely needed from the compositor: where the cursor is at
the instant the wheel opens. Everything after that arrives as normal pointer
motion on our layer surface.

`hyprctl` would do, but it is a process spawn on the critical path of a gesture;
the control socket is the same answer without the fork.
"""
from __future__ import annotations

import json
import os
import socket

_SIGNATURE = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
_RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
_SOCKET = f"{_RUNTIME}/hypr/{_SIGNATURE}/.socket.sock"


def available() -> bool:
    return bool(_SIGNATURE) and os.path.exists(_SOCKET)


def _request(command: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        sock.connect(_SOCKET)
        sock.sendall(command.encode())
        chunks = []
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def cursorpos() -> tuple[int, int] | None:
    """Cursor position in layout (logical) coordinates, matching layer-surface space."""
    if not available():
        return None
    try:
        x_text, y_text = _request("cursorpos").split(",")
        return int(x_text), int(y_text)
    except Exception:
        return None


def active_window() -> dict:
    """The focused window as a dict, or empty if nothing is focused."""
    if not available():
        return {}
    try:
        parsed = json.loads(_request("j/activewindow") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def fullscreen_active() -> bool:
    """True when the focused window is fullscreen — Rovyl's Game Mode guard."""
    return bool(active_window().get("fullscreen"))


def desktop_under_cursor() -> bool:
    """True only when the cursor is over bare wallpaper.

    A window on a visible workspace (or a pinned one, or one on a shown special
    workspace) under the cursor means no, and so does a top or overlay layer
    such as waybar. The gaps between tiled windows count as desktop, because
    that is what is showing there. Any failure answers no: when in doubt the
    middle button belongs to whatever app is under it.
    """
    if not available():
        return False
    try:
        position = cursorpos()
        if position is None:
            return False
        x, y = position

        visible = set()
        for monitor in json.loads(_request("j/monitors") or "[]"):
            visible.add(monitor["activeWorkspace"]["id"])
            special = (monitor.get("specialWorkspace") or {}).get("id", 0)
            if special:
                visible.add(special)

        for client in json.loads(_request("j/clients") or "[]"):
            if not client.get("mapped") or client.get("hidden"):
                continue
            if client["workspace"]["id"] not in visible and not client.get("pinned"):
                continue
            (left, top), (width, height) = client["at"], client["size"]
            if left <= x < left + width and top <= y < top + height:
                return False

        for monitor in json.loads(_request("j/layers") or "{}").values():
            for level in ("2", "3"):          # top and overlay; 0/1 are wallpaper
                for layer in monitor.get("levels", {}).get(level, []):
                    if layer.get("namespace") == "rota":
                        continue
                    if layer["x"] <= x < layer["x"] + layer["w"] and \
                       layer["y"] <= y < layer["y"] + layer["h"]:
                        return False
        return True
    except Exception:
        return False


def active_class() -> str:
    window = active_window()
    return (window.get("initialClass") or window.get("class") or "").lower()
