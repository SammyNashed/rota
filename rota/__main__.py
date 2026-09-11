"""Rota — entry point.

One process: the GTK loop owns the wheel, a daemon thread owns the evdev
trigger, and a second thread serves a control socket so a compositor keybind can
open the wheel without the mouse grab.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import apps, config, hypr  # noqa: E402

# gtk4-layer-shell must be loaded before libwayland-client or its surface hooks
# never install, and every layer-shell call is quietly ignored. Python imports
# Gdk (and with it libwayland) long before we could ask for the library, so the
# only reliable fix is to re-exec ourselves once with it preloaded.
_LAYER_SHELL_SO = "libgtk4-layer-shell.so.0"


def ensure_layer_shell_preloaded() -> None:
    preload = os.environ.get("LD_PRELOAD", "")
    if _LAYER_SHELL_SO in preload:
        return
    os.environ["LD_PRELOAD"] = (
        f"{_LAYER_SHELL_SO}:{preload}" if preload else _LAYER_SHELL_SO)
    # Re-exec with the original argv, not a reconstructed one: rebuilding it
    # silently drops interpreter flags such as -u, and a daemon whose stdout is
    # block-buffered looks like a daemon that never started.
    os.execv(sys.executable, sys.orig_argv)


SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "rota.sock")

# What a fresh install should already contain, in ring order. Each entry is a
# list of candidates; the first one actually installed wins.
SEED_SLICES = [
    ("Browser", ["helium", "helium-browser", "firefox", "chromium", "google-chrome"]),
    ("Terminal", ["kitty", "alacritty", "foot", "wezterm"]),
    ("Files", ["thunar", "nautilus", "org.gnome.Nautilus", "dolphin", "nemo"]),
    ("Editor", ["code", "codium", "cursor", "zed", "gnome-text-editor"]),
    ("Music", ["aura-player", "spotify", "org.gnome.Rhythmbox3"]),
    ("Settings", ["gnome-control-center", "systemsettings", "nm-connection-editor"]),
]


def seed_config(cfg: dict) -> int:
    """Fill an empty MAIN workspace with whatever is actually installed."""
    workspace = cfg["workspaces"][0]
    if workspace.get("slices"):
        return 0
    slices = []
    for label, candidates in SEED_SLICES:
        for candidate in candidates:
            found = apps.find_app(candidate)
            if found:
                slices.append({
                    "label": label,
                    "type": "desktop",
                    "target": found["target"],
                    "icon": found["icon"],
                })
                break
    workspace["slices"] = slices
    config.save(cfg)
    return len(slices)


class ControlServer(threading.Thread):
    """Accepts one-word commands so `rota open` can drive a running daemon."""

    daemon = True

    def __init__(self, handlers: dict):
        super().__init__(name="rota-control")
        self.handlers = handlers
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(SOCKET_PATH)
        self.server.listen(4)

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                command = conn.recv(64).decode(errors="replace").strip()
                handler = self.handlers.get(command)
                if handler:
                    GLib.idle_add(handler)
                    conn.sendall(b"ok")
                else:
                    conn.sendall(b"unknown")


def send_command(command: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(SOCKET_PATH)
            sock.sendall(command.encode())
            return sock.recv(16) == b"ok"
    except OSError:
        return False


def already_running() -> bool:
    """True if a daemon is already answering on the control socket.

    Two daemons both grabbing the mouse is not a theoretical problem: a stray
    instance started outside systemd's cgroup survives `systemctl stop`, keeps
    the device open, and there is nothing in the service status to suggest it.
    """
    return send_command("ping")


def run_daemon() -> int:
    if already_running():
        print("[rota] a daemon is already running; refusing to start a second one")
        return 1
    ensure_layer_shell_preloaded()
    from .trigger import MouseTrigger
    from .wheel import Wheel

    cfg = config.load()
    added = seed_config(cfg)
    if added:
        print(f"[rota] seeded MAIN with {added} slices from installed apps")

    if not hypr.available():
        print("[rota] warning: Hyprland IPC not reachable; "
              "the wheel cannot find the cursor and will not open")

    # A plain main loop, not a Gtk.Application. An application would want a
    # D-Bus name it has no use for, and failing to acquire one — after a crash,
    # or alongside a second instance — would take the launcher down with it.
    if not Gtk.init_check():
        print("[rota] could not open a display")
        return 1

    loop = GLib.MainLoop()
    holder = {}

    def on_activate():
        wheel = Wheel(cfg, on_config_change=config.save)
        holder["wheel"] = wheel

        def toggle():
            if wheel.is_open:
                wheel.close()
            else:
                wheel.open()
            return False

        def reload_config():
            fresh = config.load()
            # Mutate in place: the wheel and the trigger both hold this dict.
            for key in list(cfg):
                cfg[key] = fresh[key]
            wheel.close()
            print("[rota] config reloaded")
            return False

        ControlServer({
            "ping": lambda: False,
            "reload": reload_config,
            "open": lambda: (wheel.open(), False)[1],
            "close": lambda: (wheel.close(), False)[1],
            "toggle": toggle,
        }).start()

        general = cfg["general"]
        if general["trigger_button"] != "none":
            def suppressed() -> bool:
                """Game Mode: stay out of the way of what the user named."""
                game = cfg["game_mode"]
                if not game["enabled"]:
                    return False
                if game["auto_detect_fullscreen"] and hypr.fullscreen_active():
                    return True
                blocked = [c.strip().lower() for c in game["blocked_classes"] if c.strip()]
                return bool(blocked) and hypr.active_class() in blocked

            def guarded_open():
                if not suppressed():
                    wheel.open()
                return False

            def guarded_toggle():
                if wheel.is_open:
                    wheel.close()
                elif not suppressed():
                    wheel.open()
                return False

            trigger = MouseTrigger(
                hold_ms=general["hold_ms"],
                button=general["trigger_button"],
                on_open=lambda: GLib.idle_add(guarded_open),
                on_commit=lambda: GLib.idle_add(
                    lambda: (wheel.commit(from_gesture=True), False)[1]),
                grab=general["grab_mouse"],
                mode=general["trigger_mode"],
                on_toggle=lambda: GLib.idle_add(guarded_toggle),
                # Read cfg live, not `general`: a reload swaps the section dict.
                # An open wheel owns the button wherever the cursor is, or a
                # second hold over it would be forwarded to its own surface.
                should_trigger=lambda: (
                    wheel.is_open
                    or not cfg["general"]["desktop_only"]
                    or hypr.desktop_under_cursor()),
            )
            print(f"[rota] trigger: {trigger.setup()}")
            trigger.start()
            holder["trigger"] = trigger

        print(f"[rota] ready — hold the {general['trigger_button']} button "
              f"for {general['hold_ms']}ms, or run `rota open`")

    on_activate()
    try:
        loop.run()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if "trigger" in holder:
            holder["trigger"].stop()
            holder["trigger"].close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)


def cmd_list(cfg: dict) -> None:
    for index, workspace in enumerate(cfg["workspaces"]):
        marker = "*" if index == cfg["general"]["active_workspace"] else " "
        print(f"{marker} [{index + 1}] {workspace.get('name')}")
        for entry in workspace.get("slices") or []:
            print(f"      {entry.get('label'):<16} {entry.get('type'):<9} {entry.get('target')}")


def cmd_add(cfg: dict, argv: list[str]) -> int:
    if not argv:
        print("usage: rota add <app name|command|url> [--label L] [--workspace N]")
        return 2
    query = argv[0]
    label = None
    index = cfg["general"]["active_workspace"]
    rest = argv[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--label" and rest:
            label = rest.pop(0)
        elif flag == "--workspace" and rest:
            index = int(rest.pop(0)) - 1

    found = apps.find_app(query)
    if found:
        entry = {"label": label or found["label"], "type": "desktop",
                 "target": found["target"], "icon": found["icon"]}
    elif "://" in query or query.startswith("www."):
        entry = {"label": label or query.split("//")[-1].split("/")[0],
                 "type": "url", "target": query, "icon": "web-browser"}
    elif os.path.isdir(os.path.expanduser(query)):
        entry = {"label": label or os.path.basename(query.rstrip("/")),
                 "type": "folder", "target": query, "icon": "folder"}
    else:
        entry = {"label": label or query.split()[0], "type": "app",
                 "target": query, "icon": "application-x-executable"}

    cfg["workspaces"][index].setdefault("slices", []).append(entry)
    config.save(cfg)
    print(f"added {entry['label']} ({entry['type']}) to "
          f"{cfg['workspaces'][index]['name']}")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "daemon"

    if command in ("open", "close", "toggle", "reload"):
        if send_command(command):
            return 0
        print("[rota] daemon is not running")
        return 1

    cfg = config.load()
    if command == "list":
        cmd_list(cfg)
        return 0
    if command == "add":
        return cmd_add(cfg, argv[2:])
    if command == "apps":
        for entry in apps.installed_apps():
            print(f"{entry['target']:<44} {entry['label']}")
        return 0
    if command == "settings":
        ensure_layer_shell_preloaded()
        from . import settings
        return settings.run()
    if command == "seed":
        cfg["workspaces"][0]["slices"] = []
        print(f"seeded {seed_config(cfg)} slices")
        return 0
    if command in ("daemon", "run"):
        return run_daemon()

    print(__doc__)
    print("commands: daemon | open | close | toggle | reload | settings | add | list | apps | seed")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
