"""Installed-application discovery.

Rovyl reads the Windows Start Menu and pulls icons out of PE resources. The
freedesktop equivalent is far kinder: Gio already indexes every .desktop file on
the system, and the icon theme resolves names to real SVG/PNG assets, so none of
Rovyl's icon-quality heuristics (white-halo detection, unplated variants) are
needed here.
"""
from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GdkPixbuf, Gio, Gtk  # noqa: E402


_pixbuf_cache: dict = {}


def icon_pixbuf(icon_spec: str, size: int):
    """A GdkPixbuf at `size`, cached.

    The wheel paints its tiles in Cairo so it can scale and glow them freely,
    and Cairo cannot consume a Gtk.IconPaintable — so resolve the icon to a file
    through the theme and load that instead. SVGs come through GdkPixbuf's
    librsvg loader, so theme icons stay sharp at any size.
    """
    key = (icon_spec, size)
    if key in _pixbuf_cache:
        return _pixbuf_cache[key]

    path = None
    if icon_spec.startswith("/") and os.path.exists(icon_spec):
        path = icon_spec
    else:
        paintable = icon_paintable(icon_spec, size)
        gfile = paintable.get_file() if paintable is not None else None
        if gfile is not None:
            path = gfile.get_path()

    pixbuf = None
    if path:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
        except Exception:
            pixbuf = None
    if pixbuf is None:
        try:
            paintable = icon_paintable("application-x-executable", size)
            gfile = paintable.get_file() if paintable else None
            if gfile and gfile.get_path():
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(gfile.get_path(), size, size)
        except Exception:
            pixbuf = None

    _pixbuf_cache[key] = pixbuf
    return pixbuf


def _display():
    return Gdk.Display.get_default()


def installed_apps() -> list[dict]:
    """Every launchable app, deduped by desktop id and sorted by name."""
    seen: dict[str, dict] = {}
    for info in Gio.AppInfo.get_all():
        if not info.should_show():
            continue
        app_id = info.get_id() or ""
        if not app_id or app_id in seen:
            continue
        icon = info.get_icon()
        seen[app_id] = {
            "label": info.get_display_name() or info.get_name() or app_id,
            "type": "desktop",
            "target": app_id,
            "icon": icon.to_string() if icon else "application-x-executable",
            "description": info.get_description() or "",
        }
    return sorted(seen.values(), key=lambda a: a["label"].lower())


def find_app(query: str) -> dict | None:
    """Best match for a name or desktop id — used when seeding a fresh config."""
    query_lower = query.lower()
    candidates = installed_apps()
    for app in candidates:
        if app["target"].lower() in (query_lower, f"{query_lower}.desktop"):
            return app
    for app in candidates:
        if app["label"].lower() == query_lower:
            return app
    for app in candidates:
        if query_lower in app["label"].lower():
            return app
    return None


def icon_paintable(icon_spec: str, size: int, scale: int = 1):
    """Resolve an icon name / GIcon string / absolute path to a Gtk.IconPaintable."""
    theme = Gtk.IconTheme.get_for_display(_display())
    if icon_spec.startswith("/"):
        gicon = Gio.FileIcon.new(Gio.File.new_for_path(icon_spec))
    else:
        try:
            gicon = Gio.Icon.new_for_string(icon_spec)
        except Exception:
            gicon = Gio.ThemedIcon.new("application-x-executable")
    return theme.lookup_by_gicon(
        gicon, size, scale, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_REGULAR
    )
