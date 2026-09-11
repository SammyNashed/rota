"""The radial overlay.

A gtk4-layer-shell surface on the overlay layer, covering the monitor the cursor
is on. Layer-shell removes most of what Rovyl's Windows main process exists to
fight: there is no foreground to steal, no always-on-top race, and no DWM
flicker handshake, because the compositor does not present the surface until we
have painted it.

Two rules are carried over from Rovyl verbatim, both worth keeping:

1. Confirmation resolves from the *live* pointer, never from render state.
   Releasing mid-flight must not confirm the slice the pointer already left.
2. Highlight and confirmation share one function. Two copies of the same
   trigonometry can diverge, and lighting up one icon while opening another is
   the worst defect a launcher can have.
"""
from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

from . import apps, favicon, hud, hypr, launch  # noqa: E402

BLOOM_MS = 140.0
COLLAPSED_FRACTION = 0.34   # where slices start before they bloom outward

CSS = b"""
window.rota { background: transparent; }
.tile { }
.tile label {
  color: #ffffff;
  font-family: "Inter", "Instrument Sans", sans-serif;
  font-size: 11px;
  font-weight: 600;
  text-shadow: 0 1px 3px rgba(0,0,0,0.9);
}
.tile.active label { font-weight: 800; }
.hint {
  color: rgba(255,255,255,0.82);
  font-family: "Inter", sans-serif;
  font-size: 13px;
  text-shadow: 0 1px 4px rgba(0,0,0,0.9);
}
"""


def _hex_to_rgb(text: str) -> tuple[float, float, float]:
    text = (text or "#FFFFFF").lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (1.0, 1.0, 1.0)


def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


class Wheel:
    def __init__(self, config: dict, on_config_change=None):
        self.config = config
        self.on_config_change = on_config_change
        self.is_open = False

        self.center = (0.0, 0.0)
        self.pointer: tuple[float, float] | None = None
        self.active_index: int | None = None
        self.is_center_active = True
        self.bloom = 0.0
        self._tick_id = None
        self._bloom_start = 0.0

        self.level_items: list[dict] = []
        self.folder_stack: list[list[dict]] = []
        self.picker_mode = False
        self.tiles: list[Gtk.Widget] = []
        self.weather = hud.Weather()

        self._build_window()

    # -- construction ------------------------------------------------------
    def _build_window(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.window = Gtk.Window()
        self.window.add_css_class("rota")
        LayerShell.init_for_window(self.window)
        LayerShell.set_layer(self.window, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self.window, "rota")
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                     LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.window, edge, True)
        LayerShell.set_exclusive_zone(self.window, -1)
        LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.EXCLUSIVE)

        self.overlay = Gtk.Overlay()
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_draw_func(self._draw)
        self.overlay.set_child(self.canvas)

        self.window.set_child(self.overlay)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.window.add_controller(motion)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._on_click)
        self.window.add_controller(click)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.window.add_controller(keys)

    # -- geometry ----------------------------------------------------------
    @property
    def _appearance(self) -> dict:
        return self.config["appearance"]

    def _icon_size(self) -> int:
        count = max(len(self.level_items), 1)
        base = self._appearance["icon_size"]
        # Shrink a little as the ring fills up, the way Rovyl does, so a busy
        # wheel stays readable instead of overlapping.
        density = min(count / 12.0, 1.0)
        return max(int(base * (1.0 - 0.18 * density)), 28)

    def _radius(self) -> float:
        count = max(len(self.level_items), 1)
        icon = self._icon_size()
        spacing = self._appearance["app_spacing"]
        # Never let neighbouring icons touch: the ring has to be long enough to
        # seat every tile plus its gap.
        needed = (count * (icon + spacing)) / (2 * math.pi)
        # And never let a slice sit inside the dead zone, or it could not be
        # aimed at without cancelling.
        clear = self._appearance["activation_threshold"] + icon / 2 + 8
        return max(self._appearance["menu_radius"], needed, clear)

    def _dead_zone(self) -> float:
        hub = self._icon_size() * 0.82 * 1.2
        return max(self._appearance["activation_threshold"], hub * 0.5)

    def resolve_aim(self, point) -> tuple[bool, int | None]:
        """The single source of truth for both highlight and confirmation."""
        if point is None:
            return True, None
        count = len(self.level_items)
        cx, cy = self.center
        dx, dy = point[0] - cx, point[1] - cy
        if math.hypot(dx, dy) < self._dead_zone():
            return True, None
        if count == 0:
            return False, None

        slice_angle = 360.0 / count

        if self.config["general"]["selection_mode"] == "cursor":
            # Only the icon actually under the pointer lights up, and releasing
            # away from every icon cancels.
            radius = self._radius()
            hit = max(self._icon_size() * 0.85, 22)
            nearest, nearest_distance = None, float("inf")
            for i in range(count):
                rad = math.radians(i * slice_angle - 90)
                distance = math.hypot(dx - radius * math.cos(rad),
                                      dy - radius * math.sin(rad))
                if distance < nearest_distance:
                    nearest, nearest_distance = i, distance
            return False, (nearest if nearest_distance <= hit else None)

        # Angle mode with a reach limit. Upstream has none — a slice stays aimed
        # with the cursor at the far edge of the screen — but that leaves no way
        # to un-aim except returning to the centre, so overshooting a slice you
        # did not want keeps it lit and launches it on release.
        reach = self._appearance["aim_reach"]
        if reach > 0:
            limit = self._radius() + self._icon_size() / 2 + reach
            if math.hypot(dx, dy) > limit:
                return False, None

        angle = math.degrees(math.atan2(dy, dx)) + 90.0
        if angle < 0:
            angle += 360.0
        index = int(((angle + slice_angle / 2) % 360) / slice_angle)
        return False, (index if 0 <= index < count else None)

    # -- opening / closing -------------------------------------------------
    def open(self) -> None:
        if self.is_open:
            return
        position = hypr.cursorpos()
        if position is None:
            return

        monitor = self._monitor_at(position)
        if monitor is not None:
            LayerShell.set_monitor(self.window, monitor)
            area = monitor.get_geometry()
            self.center = (position[0] - area.x, position[1] - area.y)
        else:
            self.center = (float(position[0]), float(position[1]))

        self.folder_stack = []
        self.picker_mode = False
        self.level_items = self._root_items()

        # A fresh opening with the pointer yet to move. Leaving the previous
        # gesture's pointer here would let a release without any movement
        # confirm a slice. None resolves to the centre, which cancels.
        self.pointer = None
        self.is_center_active = True
        self.active_index = None

        self._rebuild_tiles()
        self.is_open = True
        self.window.present()
        self._start_bloom()

    def close(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        if self._tick_id is not None:
            self.window.remove_tick_callback(self._tick_id)
            self._tick_id = None
        self.window.set_visible(False)

    def _monitor_at(self, position):
        display = Gdk.Display.get_default()
        for monitor in display.get_monitors():
            area = monitor.get_geometry()
            if area.x <= position[0] < area.x + area.width and \
               area.y <= position[1] < area.y + area.height:
                return monitor
        monitors = display.get_monitors()
        return monitors[0] if monitors.get_n_items() else None

    # -- levels ------------------------------------------------------------
    def _workspaces(self) -> list[dict]:
        return self.config["workspaces"]

    def _active_workspace(self) -> dict:
        index = self.config["general"]["active_workspace"]
        spaces = self._workspaces()
        if not spaces:
            return {"name": "MAIN", "slices": []}
        return spaces[max(0, min(index, len(spaces) - 1))]

    def _root_items(self) -> list[dict]:
        return list(self._active_workspace().get("slices") or [])

    def _picker_items(self) -> list[dict]:
        return [
            {"label": ws.get("name", f"WS{i+1}"), "type": "__workspace__",
             "target": str(i), "icon": ws.get("icon", "view-grid-symbolic"),
             "description": f"({i + 1})"}
            for i, ws in enumerate(self._workspaces())
        ]

    def _enter_level(self, items: list[dict], picker: bool = False) -> None:
        self.level_items = items
        self.picker_mode = picker
        self._rebuild_tiles()
        # Changing level swaps the slices under a cursor that did not move, and
        # the highlight is only recomputed on motion — so re-aim now, or the new
        # level arrives entirely unlit until the mouse is nudged.
        self.is_center_active, self.active_index = self.resolve_aim(self.pointer)
        self._start_bloom()

    # -- painting ----------------------------------------------------------
    def _rebuild_tiles(self) -> None:
        """Nothing to build: slices are painted by `_draw`, not widgets.

        They were GTK widgets in a Gtk.Fixed until the restyle. Cairo won the
        job because the active slice has to scale and cast a coloured halo, and
        resizing a widget every frame reallocates the whole overlay.
        """
        self.tiles = []

    def _layout_tiles(self) -> None:
        self.canvas.queue_draw()

    # -- painting primitives ----------------------------------------------
    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r) -> None:
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _drop_shadow(self, cr, x, y, w, h, r, spread: float, alpha: float) -> None:
        """A soft shadow, faked in layers.

        Cairo has no blur, and running a real one per frame on the paint thread
        is not worth it here: a handful of expanding rounded rects at low alpha
        is indistinguishable at this size.
        """
        layers = 6
        for i in range(layers, 0, -1):
            grow = spread * i / layers
            cr.set_source_rgba(0, 0, 0, alpha / layers)
            self._rounded_rect(cr, x - grow, y - grow + spread * 0.35,
                               w + grow * 2, h + grow * 2, r + grow)
            cr.fill()

    def _tile_geometry(self, index: int, count: int, radius: float):
        rad = math.radians(index * (360.0 / count) - 90)
        return (self.center[0] + radius * math.cos(rad),
                self.center[1] + radius * math.sin(rad))

    def _draw(self, _area, cr, width, height) -> None:
        appearance = self._appearance
        eased = _ease_out_cubic(self.bloom)
        cx, cy = self.center
        hover = _hex_to_rgb(appearance["hover_color"])

        options = cairo.FontOptions()
        options.set_antialias(cairo.Antialias.GRAY)
        cr.set_font_options(options)

        # Optional scrim. Rovyl leans on each tile being opaque enough to read
        # on any wallpaper rather than on dimming the desktop, so this defaults
        # to nothing.
        dim = appearance["backdrop_opacity"] * eased
        if dim > 0.01:
            if appearance["background_style"] == "circle":
                plate = self._radius() + self._icon_size() * 1.4
                gradient = cairo.RadialGradient(cx, cy, plate * 0.5, cx, cy, plate)
                gradient.add_color_stop_rgba(0, 0, 0, 0, dim)
                gradient.add_color_stop_rgba(1, 0, 0, 0, 0)
                cr.set_source(gradient)
                cr.arc(cx, cy, plate, 0, 2 * math.pi)
                cr.fill()
            else:
                cr.set_source_rgba(0, 0, 0, dim)
                cr.rectangle(0, 0, width, height)
                cr.fill()

        count = len(self.level_items)
        icon = self._icon_size()
        radius = self._radius() * (COLLAPSED_FRACTION + (1 - COLLAPSED_FRACTION) * eased)

        # Inactive tiles first, so the active one — which is larger and casts a
        # ring — always paints over its neighbours.
        order = [i for i in range(count) if i != self.active_index]
        if self.active_index is not None and not self.is_center_active:
            order.append(self.active_index)

        for i in order:
            self._draw_tile(cr, i, count, radius, icon, eased, hover)

        self._draw_hub(cr, cx, cy, eased, hover)
        if not self.level_items:
            self._draw_empty_hint(cr, cx, cy, eased)
        self._draw_breadcrumb(cr, cx, cy, eased)
        self._draw_hud(cr, width, height, eased)

    def _draw_tile(self, cr, index, count, radius, icon, eased, hover) -> None:
        item = self.level_items[index]
        active = (index == self.active_index and not self.is_center_active)
        x, y = self._tile_geometry(index, count, radius)

        # The active tile grows. That, not a highlight wedge, is how Rovyl says
        # "this one" — it survives any wallpaper behind it.
        scale = (1.22 if active else 1.0) * (0.55 + 0.45 * eased)
        size = icon * scale
        left, top = x - size / 2, y - size / 2
        corner = size * 0.28

        self._drop_shadow(cr, left, top, size, size, corner,
                          size * (0.22 if active else 0.14),
                          (0.5 if active else 0.42) * eased)

        if active:
            # A soft ring of the hover colour, standing in for the CSS
            # `0 0 0 5px <hover>24` halo.
            cr.set_source_rgba(*hover, 0.16 * eased)
            self._rounded_rect(cr, left - 5, top - 5, size + 10, size + 10, corner + 5)
            cr.fill()

        # Near-opaque plate: the tile has to hold up over a desktop we do not
        # control, without relying on a global scrim.
        if active:
            cr.set_source_rgba(*hover, 0.985 * eased)
        else:
            base = 0.06 + self._appearance["backdrop_opacity"] * 0.04
            cr.set_source_rgba(base, base, base + 0.008, 0.985 * eased)
        self._rounded_rect(cr, left, top, size, size, corner)
        cr.fill()

        pixbuf = apps.icon_pixbuf(self._icon_spec(item), max(int(icon), 8))
        if pixbuf is not None:
            inset = size * 0.16
            target = size - inset * 2
            factor = target / pixbuf.get_width()
            cr.save()
            cr.translate(left + inset, top + inset)
            cr.scale(factor, factor)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint_with_alpha(eased)
            cr.restore()

        # Double contour: a light border inside, a dark ring outside. One of the
        # two always reads, whatever the tile is sitting on.
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.5 * eased)
        self._rounded_rect(cr, left - 0.5, top - 0.5, size + 1, size + 1, corner + 0.5)
        cr.stroke()
        if active:
            cr.set_source_rgba(*hover, eased)
        else:
            cr.set_source_rgba(1, 1, 1, (0.28 + self._appearance["backdrop_opacity"] * 0.08) * eased)
        self._rounded_rect(cr, left + 0.5, top + 0.5, size - 1, size - 1, corner)
        cr.stroke()

        if active and self._appearance["show_labels"]:
            self._draw_label_pill(cr, item, x, y, size, eased, hover)

    def _icon_spec(self, item: dict) -> str:
        spec = item.get("icon") or "application-x-executable"
        if item.get("type") == "url":
            return favicon.cached_path(item.get("target", "")) or spec
        return spec

    def _draw_label_pill(self, cr, item, x, y, size, eased, hover) -> None:
        """Only the aimed-at slice is named, as a pill pushed away from the hub."""
        text = item.get("label", "")
        if not text:
            return
        cr.select_font_face("Inter", cairo.FontSlant.NORMAL, cairo.FontWeight.BOLD)
        cr.set_font_size(12)
        extents = cr.text_extents(text)
        pad_x, pad_y = 9, 5
        w = extents.width + pad_x * 2
        h = extents.height + pad_y * 2

        cx, cy = self.center
        # Push it outward along the slice's own direction, so the pill never
        # lands on top of the hub or its neighbours.
        dx, dy = x - cx, y - cy
        length = math.hypot(dx, dy) or 1.0
        px = x + (dx / length) * (size / 2 + w / 2 + 8)
        py = y + (dy / length) * (size / 2 + h / 2 + 6)

        self._drop_shadow(cr, px - w / 2, py - h / 2, w, h, h / 2, 6, 0.4 * eased)
        cr.set_source_rgba(*hover, 0.97 * eased)
        self._rounded_rect(cr, px - w / 2, py - h / 2, w, h, h / 2)
        cr.fill()

        luminance = 0.2126 * hover[0] + 0.7152 * hover[1] + 0.0722 * hover[2]
        ink = (0.04, 0.04, 0.05) if luminance > 0.5 else (1, 1, 1)
        cr.set_source_rgba(*ink, eased)
        cr.move_to(px - extents.width / 2 - extents.x_bearing,
                   py - extents.height / 2 - extents.y_bearing)
        cr.show_text(text)

    def _draw_hub(self, cr, cx, cy, eased, hover) -> None:
        # Sized against the tiles, not the dead zone: the dead zone is a hit
        # target and is deliberately larger than anything drawn.
        r = self._icon_size() * 0.40 * (0.55 + 0.45 * eased)
        self._drop_shadow(cr, cx - r, cy - r, r * 2, r * 2, r, r * 0.22, 0.4 * eased)
        # Near-opaque like the tiles. At 0.8 alpha the wallpaper bled through and
        # the hub picked up its colour instead of reading as a dark object.
        alpha = 0.75 + 0.235 * self._appearance["menu_opacity"]
        cr.set_source_rgba(0.055, 0.055, 0.062, alpha * eased)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(1)
        cr.set_source_rgba(*hover, (0.8 if self.is_center_active else 0.22) * eased)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()

        # A back arrow when there is somewhere to go back to, otherwise a dot.
        cr.set_source_rgba(1, 1, 1, (0.9 if self.is_center_active else 0.45) * eased)
        cr.set_line_width(1.8)
        if self.folder_stack or self.picker_mode:
            a = r * 0.42
            cr.arc(cx, cy + a * 0.15, a, math.pi * 0.85, math.pi * 2.05)
            cr.stroke()
            cr.move_to(cx - a * 1.05, cy - a * 0.35)
            cr.line_to(cx - a * 0.45, cy - a * 0.5)
            cr.line_to(cx - a * 0.75, cy + a * 0.2)
            cr.close_path()
            cr.fill()
        else:
            cr.arc(cx, cy, max(r * 0.12, 2), 0, 2 * math.pi)
            cr.fill()

    def _draw_breadcrumb(self, cr, cx, cy, eased) -> None:
        """The 'Main · Back' pill that sits under the wheel."""
        name = self._active_workspace().get("name", "MAIN")
        if self.picker_mode:
            name = "WORKSPACE"
        crumbs = [name] + (["Back"] if (self.folder_stack or self.picker_mode) else [])

        cr.select_font_face("Inter", cairo.FontSlant.NORMAL, cairo.FontWeight.BOLD)
        cr.set_font_size(11)
        widths = [cr.text_extents(c).width for c in crumbs]
        gap, pad_x, h = 6, 10, 22
        w = sum(widths) + pad_x * 2 * len(crumbs) + gap * (len(crumbs) - 1)
        y = cy + self._radius() + self._icon_size() * 0.95
        x = cx - w / 2

        self._drop_shadow(cr, x, y - h / 2, w, h, h / 2, 7, 0.42 * eased)
        cr.set_source_rgba(0.055, 0.055, 0.062, 0.95 * eased)
        self._rounded_rect(cr, x, y - h / 2, w, h, h / 2)
        cr.fill()

        cursor = x
        for i, crumb in enumerate(crumbs):
            chip_w = widths[i] + pad_x * 2
            if i > 0:
                cr.set_source_rgba(1, 1, 1, 0.07 * eased)
                self._rounded_rect(cr, cursor + 2, y - h / 2 + 4, chip_w - 4, h - 8, (h - 8) / 2)
                cr.fill()
            cr.set_source_rgba(1, 1, 1, (0.92 if i == 0 else 0.5) * eased)
            extents = cr.text_extents(crumb)
            cr.move_to(cursor + pad_x - extents.x_bearing,
                       y - extents.height / 2 - extents.y_bearing)
            cr.show_text(crumb)
            cursor += chip_w + gap

    def _draw_empty_hint(self, cr, cx, cy, eased) -> None:
        hint = "no slices yet — run: rota settings"
        cr.select_font_face("Inter")
        cr.set_font_size(12)
        extents = cr.text_extents(hint)
        cr.set_source_rgba(1, 1, 1, 0.6 * eased)
        cr.move_to(cx - extents.width / 2, cy + self._radius() + 30)
        cr.show_text(hint)

    def _draw_hud(self, cr, width, height, eased) -> None:
        parts = hud.lines(self.config, self.weather)
        if not parts:
            return
        text = "   ".join(parts)
        cr.select_font_face("Inter", cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)
        cr.set_font_size(28)
        extents = cr.text_extents(text)
        margin = 36
        vertical, _, horizontal = self.config["hud"]["clock_position"].partition("-")
        x = {"left": margin,
             "center": (width - extents.width) / 2,
             "right": width - extents.width - margin}.get(horizontal, margin)
        y = margin + extents.height if vertical == "top" else height - margin
        cr.set_source_rgba(0, 0, 0, 0.55 * eased)
        cr.move_to(x + 1, y + 1); cr.show_text(text)
        cr.set_source_rgba(1, 1, 1, 0.92 * eased)
        cr.move_to(x, y); cr.show_text(text)

    # -- animation ---------------------------------------------------------
    def _start_bloom(self) -> None:
        self.bloom = 0.0
        self._bloom_start = GLib.get_monotonic_time() / 1000.0
        if self._tick_id is not None:
            self.window.remove_tick_callback(self._tick_id)
        self._tick_id = self.window.add_tick_callback(self._on_tick)

    def _on_tick(self, _widget, _clock) -> bool:
        elapsed = GLib.get_monotonic_time() / 1000.0 - self._bloom_start
        self.bloom = min(elapsed / BLOOM_MS, 1.0)
        self._layout_tiles()
        self.canvas.queue_draw()
        if self.bloom >= 1.0:
            self._tick_id = None
            return False
        return True

    # -- input -------------------------------------------------------------
    def _on_motion(self, _controller, x, y) -> None:
        self.pointer = (x, y)
        is_center, index = self.resolve_aim(self.pointer)
        if is_center != self.is_center_active or index != self.active_index:
            self.is_center_active, self.active_index = is_center, index
            self._layout_tiles()
            self.canvas.queue_draw()

    def _on_click(self, gesture, _n, x, y) -> None:
        self.pointer = (x, y)
        if gesture.get_current_button() == 3:   # right click steps back out
            self._go_back()
            return
        self.commit()

    def _on_key(self, _controller, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self.folder_stack or self.picker_mode:
                self._go_back()
            else:
                self.close()
            return True
        if keyval in (Gdk.KEY_BackSpace, Gdk.KEY_Left):
            self._go_back()
            return True
        if Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
            self._switch_workspace(keyval - Gdk.KEY_1)
            return True
        return False

    def _go_back(self) -> None:
        """One step back, whatever "back" currently means.

        The workspace list is the level *above* a workspace, so backing out of
        a workspace root goes there — whether or not you switched to get in, and
        whether or not the workspace has anything in it. The list is the top, so
        backing out of it closes.
        """
        if self.picker_mode:
            self.close()
        elif self.folder_stack:
            self._enter_level(self.folder_stack.pop())
        elif len(self._workspaces()) > 1:
            self._enter_level(self._picker_items(), picker=True)
        else:
            self.close()

    def _switch_workspace(self, index: int) -> None:
        spaces = self._workspaces()
        if not (0 <= index < len(spaces)):
            return
        self.config["general"]["active_workspace"] = index
        if self.on_config_change:
            self.on_config_change(self.config)
        self.folder_stack = []
        self.picker_mode = False
        self._enter_level(self._root_items())

    # -- confirmation ------------------------------------------------------
    def commit(self, from_gesture: bool = False) -> None:
        """Resolve from the live pointer, never from render state.

        `from_gesture` means the trigger button was released, as opposed to a
        deliberate left click. Ending a gesture over the hub is how you abandon
        one — the pointer simply never left the middle — so it cancels and never
        activates the centre. The centre only acts on a real click.
        """
        if not self.is_open:
            return
        is_center, index = self.resolve_aim(self.pointer)

        if is_center and from_gesture:
            # Releasing over the hub is "undo", not "choose". One level down it
            # steps back out and leaves the wheel up, so you can carry straight
            # on — hold the button again to aim, or just click. Only at the top
            # level, where there is nothing to undo, does it close.
            if self.folder_stack or self.picker_mode:
                self._go_back()
            else:
                self.close()
            return

        if is_center:
            if self.folder_stack or self.picker_mode:
                self._go_back()
                return
            action = self.config["center"]["action"]
            if action == "launch" and self.config["center"]["target"]:
                centre = self.config["center"]
                self.close()
                ok, message = launch.launch({"type": centre.get("type", "app"),
                                             "target": centre["target"]})
                if not ok:
                    print(f"[rota] centre launch failed: {message}")
            elif action == "picker" and len(self._workspaces()) > 1:
                self._enter_level(self._picker_items(), picker=True)
            else:
                self.close()
            return

        if index is None or index >= len(self.level_items):
            self.close()
            return

        item = self.level_items[index]
        kind = item.get("type", "app")

        if kind == "__workspace__":
            self._switch_workspace(int(item["target"]))
            return

        # "submenu" is a nested wheel; "folder" is a directory to open in the
        # file manager. They were the same word once and it silently made every
        # directory slice unopenable.
        if kind == "submenu":
            self.folder_stack.append(self.level_items)
            self._enter_level(list(item.get("children") or []))
            return

        self.close()
        ok, message = launch.launch(item)
        if not ok:
            print(f"[rota] launch failed: {item.get('label')}: {message}")
