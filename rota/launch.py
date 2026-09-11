"""Launching a slice target.

Rovyl's win32-launch.js is mostly quoting rules for cmd.exe. Here the work is
different: pick the right freedesktop mechanism per target type, and make sure
the child does not inherit this process's environment wholesale.
"""
from __future__ import annotations

import os
import shlex
import subprocess

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # noqa: E402

# Anything a Claude Code session (or our own daemon) exports that would change how
# a launched GUI app behaves. An app started from the wheel must look like an app
# started from the desktop.
_ENV_BLOCKLIST = (
    "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "TERM",
    "CLAUDE_CODE_SESSION", "CLAUDECODE", "CLAUDE_SESSION_ID",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME",
    "ROVYL_DAEMON",
)


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_BLOCKLIST}
    env.setdefault("TERM", "xterm-256color")
    return env


def _terminal_argv(command: str) -> list[str]:
    for candidate, flag in (
        (os.environ.get("TERMINAL"), "-e"),
        ("kitty", "-e"), ("alacritty", "-e"), ("foot", "-e"),
        ("wezterm", "-e"), ("xterm", "-e"),
    ):
        if candidate and GLib.find_program_in_path(candidate):
            return [candidate, flag] + shlex.split(command)
    return shlex.split(command)


def _spawn(argv: list[str]) -> None:
    # start_new_session detaches the child, so it survives the daemon restarting
    # and never receives the daemon's signals.
    subprocess.Popen(
        argv,
        env=clean_env(),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launch(slice_def: dict) -> tuple[bool, str]:
    """Run one slice. Returns (ok, message)."""
    kind = slice_def.get("type", "app")
    target = (slice_def.get("target") or "").strip()
    if not target:
        return False, "empty target"

    try:
        if kind == "desktop":
            info = Gio.DesktopAppInfo.new(target)
            if info is None:
                return False, f"no such desktop entry: {target}"
            # DesktopAppInfo puts the child in its own systemd scope and emits
            # startup notification, which a bare exec would not.
            info.launch([], None)
            return True, target

        if kind == "url":
            url = target if "://" in target else f"https://{target}"
            _spawn(["xdg-open", url])
            return True, url

        if kind == "folder":
            path = os.path.expanduser(target)
            if not os.path.isdir(path):
                return False, f"no such folder: {path}"
            _spawn(["xdg-open", path])
            return True, path

        if kind == "terminal":
            _spawn(_terminal_argv(target))
            return True, target

        # "app": a raw command line.
        argv = shlex.split(target)
        if not argv:
            return False, "empty command"
        if not GLib.find_program_in_path(argv[0]) and not os.path.isabs(argv[0]):
            return False, f"not on PATH: {argv[0]}"
        _spawn(argv)
        return True, target

    except Exception as exc:
        return False, str(exc)
