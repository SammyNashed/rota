"""The settings window.

Changes are written and applied as you make them — there is no Save button —
because the wheel is a thing you tune by looking at it. Every write is debounced
and followed by a `reload` on the daemon's control socket, so the running
launcher picks the change up without a restart.
"""
from __future__ import annotations

import shutil

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import apps, config  # noqa: E402

SAVE_DEBOUNCE_MS = 400

SLICE_KINDS = [
    ("desktop", "Installed app"),
    ("app", "Command"),
    ("url", "Website"),
    ("folder", "Folder"),
    ("terminal", "Terminal command"),
    ("submenu", "Sub-wheel"),
]


def _combo(strings: list[str]) -> Gtk.StringList:
    model = Gtk.StringList()
    for text in strings:
        model.append(text)
    return model


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Rota")
        self.set_default_size(720, 780)
        self.cfg = config.load()
        self._save_source = None
        self._workspace_index = self.cfg["general"]["active_workspace"]
        self._path: list[int] = []   # indices into nested sub-wheels

        self.add(self._wheel_page())
        self.add(self._appearance_page())
        self.add(self._trigger_page())
        self.add(self._hud_page())
        self.add(self._advanced_page())
        self._refresh_slices()

    # -- persistence -------------------------------------------------------
    def _save(self) -> None:
        """Debounced: dragging a slider must not write the file 60 times a second."""
        if self._save_source is not None:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(SAVE_DEBOUNCE_MS, self._save_now)

    def _save_now(self) -> bool:
        self._save_source = None
        config.save(self.cfg)
        from .__main__ import send_command
        send_command("reload")
        return False

    def _bind_switch(self, row, section: str, key: str) -> None:
        row.set_active(bool(self.cfg[section][key]))
        row.connect("notify::active",
                    lambda r, _p: (self.cfg[section].__setitem__(key, r.get_active()),
                                   self._save()))

    def _bind_spin(self, row, section: str, key: str) -> None:
        row.set_value(float(self.cfg[section][key]))
        row.connect("notify::value",
                    lambda r, _p: (self.cfg[section].__setitem__(key, int(r.get_value())),
                                   self._save()))

    def _bind_combo(self, row, section: str, key: str, values: list[str]) -> None:
        current = self.cfg[section][key]
        row.set_selected(values.index(current) if current in values else 0)
        row.connect("notify::selected",
                    lambda r, _p: (self.cfg[section].__setitem__(key, values[r.get_selected()]),
                                   self._save()))

    def _bind_entry(self, row, section: str, key: str) -> None:
        row.set_text(str(self.cfg[section][key] or ""))
        row.connect("changed",
                    lambda r: (self.cfg[section].__setitem__(key, r.get_text()),
                               self._save()))

    # -- the wheel page ----------------------------------------------------
    def _wheel_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Wheel", icon_name="view-grid-symbolic")

        group = Adw.PreferencesGroup(title="Workspace")
        names = [w.get("name", f"WS{i+1}") for i, w in enumerate(self.cfg["workspaces"])]
        self.workspace_row = Adw.ComboRow(title="Editing", model=_combo(names))
        self.workspace_row.set_selected(self._workspace_index)
        self.workspace_row.connect("notify::selected", self._on_workspace_changed)
        group.add(self.workspace_row)

        rename = Adw.EntryRow(title="Name")
        rename.set_text(names[self._workspace_index])
        rename.connect("changed", self._on_rename)
        self.rename_row = rename
        group.add(rename)
        page.add(group)

        self.slice_group = Adw.PreferencesGroup(title="Slices")
        header = Gtk.Box(spacing=6)
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic",
                                      tooltip_text="Leave this sub-wheel")
        self.back_button.connect("clicked", self._on_leave_submenu)
        self.back_button.set_visible(False)
        header.append(self.back_button)
        add_button = Gtk.MenuButton(icon_name="list-add-symbolic", tooltip_text="Add a slice")
        menu = Gio.Menu()
        for kind, label in SLICE_KINDS:
            menu.append(label, f"win.add::{kind}")
        add_button.set_menu_model(menu)
        header.append(add_button)
        self.slice_group.set_header_suffix(header)
        page.add(self.slice_group)

        # Adw.PreferencesWindow is a Gtk.Window, not an ApplicationWindow, so it
        # is not itself an action map — the menu needs a group inserted here.
        action = Gio.SimpleAction.new("add", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, param: self._add_slice(param.get_string()))
        group = Gio.SimpleActionGroup()
        group.add_action(action)
        self.insert_action_group("win", group)
        return page

    def _slices(self) -> list[dict]:
        """The list currently being edited, following the sub-wheel path."""
        items = self.cfg["workspaces"][self._workspace_index].setdefault("slices", [])
        for index in self._path:
            items = items[index].setdefault("children", [])
        return items

    def _refresh_slices(self) -> None:
        for row in getattr(self, "_rows", []):
            self.slice_group.remove(row)
        self._rows = []
        self.back_button.set_visible(bool(self._path))

        items = self._slices()
        if not items:
            row = Adw.ActionRow(title="No slices here",
                                subtitle="Use + to add an app, a website or a folder")
            self.slice_group.add(row)
            self._rows.append(row)
            return

        for index, item in enumerate(items):
            row = Adw.ActionRow(title=item.get("label") or "(unnamed)",
                                subtitle=self._describe(item))
            icon = Gtk.Image(pixel_size=32)
            paintable = apps.icon_paintable(item.get("icon") or "application-x-executable", 32)
            if paintable:
                icon.set_from_paintable(paintable)
            row.add_prefix(icon)

            box = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
            if item.get("type") == "submenu":
                enter = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Open sub-wheel")
                enter.add_css_class("flat")
                enter.connect("clicked", lambda _b, i=index: self._on_enter_submenu(i))
                box.append(enter)
            for icon_name, tip, handler in (
                ("go-up-symbolic", "Move up", self._move_up),
                ("go-down-symbolic", "Move down", self._move_down),
                ("document-edit-symbolic", "Edit", self._edit_slice),
                ("user-trash-symbolic", "Remove", self._remove_slice),
            ):
                button = Gtk.Button(icon_name=icon_name, tooltip_text=tip)
                button.add_css_class("flat")
                button.connect("clicked", lambda _b, i=index, h=handler: h(i))
                box.append(button)
            row.add_suffix(box)
            self.slice_group.add(row)
            self._rows.append(row)

    def _describe(self, item: dict) -> str:
        kind = item.get("type", "app")
        if kind == "submenu":
            return f"sub-wheel · {len(item.get('children') or [])} slices"
        return f"{kind} · {item.get('target', '')}"

    # -- slice actions -----------------------------------------------------
    def _on_workspace_changed(self, row, _param) -> None:
        self._workspace_index = row.get_selected()
        self._path = []
        self.rename_row.set_text(
            self.cfg["workspaces"][self._workspace_index].get("name", ""))
        self._refresh_slices()

    def _on_rename(self, row) -> None:
        name = row.get_text().strip() or f"WS{self._workspace_index + 1}"
        self.cfg["workspaces"][self._workspace_index]["name"] = name
        self._save()

    def _on_enter_submenu(self, index: int) -> None:
        self._path.append(index)
        self._refresh_slices()

    def _on_leave_submenu(self, _button) -> None:
        if self._path:
            self._path.pop()
        self._refresh_slices()

    def _move_up(self, index: int) -> None:
        items = self._slices()
        if index > 0:
            items[index - 1], items[index] = items[index], items[index - 1]
            self._save(); self._refresh_slices()

    def _move_down(self, index: int) -> None:
        items = self._slices()
        if index < len(items) - 1:
            items[index + 1], items[index] = items[index], items[index + 1]
            self._save(); self._refresh_slices()

    def _remove_slice(self, index: int) -> None:
        del self._slices()[index]
        self._save(); self._refresh_slices()

    def _add_slice(self, kind: str) -> None:
        if kind == "desktop":
            if shutil.which("rofi"):
                self._pick_app_via_rofi(self._app_picked_rofi)
            else:
                self._choose_app()
            return
        blank = {
            "submenu": {"label": "New sub-wheel", "type": "submenu",
                        "target": "", "icon": "folder", "children": []},
            "url": {"label": "Website", "type": "url",
                    "target": "https://", "icon": "web-browser"},
            "folder": {"label": "Folder", "type": "folder",
                       "target": "~", "icon": "folder"},
            "terminal": {"label": "Command", "type": "terminal",
                         "target": "htop", "icon": "utilities-terminal"},
            "app": {"label": "Command", "type": "app",
                    "target": "", "icon": "application-x-executable"},
        }[kind]
        self._slices().append(dict(blank))
        self._save(); self._refresh_slices()
        self._edit_slice(len(self._slices()) - 1)

    def _pick_app_via_rofi(self, callback) -> None:
        """Run Rofi as an app picker; calls `callback` with the chosen entry or None.

        Rofi's own drun mode launches apps directly and has no "give me back
        the selection" output, so instead we drive it in -dmenu mode with our
        own entry list (same source as the GTK picker: Gio.AppInfo) and ask
        it to hand back the chosen index.
        """
        entries = apps.installed_apps()
        if not entries:
            callback(None)
            return
        # Each line's icon metadata is NUL-separated ("label\0icon\x1fname"),
        # so this has to go over stdin as raw bytes: communicate_utf8_async
        # treats the buffer as a C string and silently truncates at the
        # first embedded NUL, which only ever fed Rofi the first app.
        lines = [f"{e['label']}\0icon\x1f{e.get('icon') or 'application-x-executable'}"
                 for e in entries]
        stdin_bytes = ("\n".join(lines) + "\n").encode("utf-8")

        try:
            proc = Gio.Subprocess.new(
                ["rofi", "-dmenu", "-i", "-show-icons", "-format", "i",
                 "-p", "Add app to wheel"],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
        except GLib.Error:
            callback(None)
            return

        def on_done(source, result) -> None:
            try:
                ok, stdout_buf, _stderr_buf = source.communicate_finish(result)
            except GLib.Error:
                callback(None)
                return
            text = (stdout_buf.get_data().decode("utf-8") if stdout_buf else "").strip()
            if not (ok and source.get_successful() and text.isdigit()):
                callback(None)
                return
            index = int(text)
            callback(entries[index] if 0 <= index < len(entries) else None)

        proc.communicate_async(GLib.Bytes.new(stdin_bytes), None, on_done)

    def _app_picked_rofi(self, entry: dict | None) -> None:
        if entry is None:
            return
        self._slices().append({"label": entry["label"], "type": "desktop",
                               "target": entry["target"], "icon": entry["icon"]})
        self._save(); self._refresh_slices()

    def _choose_app(self) -> None:
        dialog = Adw.Window(transient_for=self, modal=True,
                            title="Choose an app", default_width=440, default_height=560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Adw.HeaderBar())
        search = Gtk.SearchEntry(placeholder_text="Search installed apps", margin_top=6,
                                 margin_start=12, margin_end=12, margin_bottom=6)
        box.append(search)

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True, margin_start=12,
                                      margin_end=12, margin_bottom=12)
        scroller.set_child(listbox)
        box.append(scroller)
        dialog.set_content(box)

        entries = apps.installed_apps()

        def populate(term: str = "") -> None:
            while (child := listbox.get_first_child()) is not None:
                listbox.remove(child)
            term = term.lower()
            for entry in entries:
                if term and term not in entry["label"].lower():
                    continue
                row = Adw.ActionRow(title=entry["label"], subtitle=entry["target"],
                                    activatable=True)
                image = Gtk.Image(pixel_size=32)
                paintable = apps.icon_paintable(entry["icon"], 32)
                if paintable:
                    image.set_from_paintable(paintable)
                row.add_prefix(image)
                row.connect("activated", lambda _r, e=entry: self._app_picked(dialog, e))
                listbox.append(row)

        search.connect("search-changed", lambda s: populate(s.get_text()))
        populate()
        dialog.present()

    def _app_picked(self, dialog, entry: dict) -> None:
        self._slices().append({"label": entry["label"], "type": "desktop",
                               "target": entry["target"], "icon": entry["icon"]})
        self._save(); self._refresh_slices()
        dialog.close()

    def _edit_slice(self, index: int) -> None:
        item = self._slices()[index]
        dialog = Adw.Window(transient_for=self, modal=True, title="Edit slice",
                            default_width=460)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Adw.HeaderBar())
        group = Adw.PreferencesGroup(margin_top=12, margin_bottom=12,
                                     margin_start=12, margin_end=12)

        label_row = Adw.EntryRow(title="Label")
        label_row.set_text(item.get("label", ""))
        group.add(label_row)

        target_row = Adw.EntryRow(title="Target")
        target_row.set_text(item.get("target", ""))
        group.add(target_row)

        icon_row = Adw.EntryRow(title="Icon name or path")
        icon_row.set_text(item.get("icon", ""))
        group.add(icon_row)

        box.append(group)

        def apply(*_a) -> None:
            item["label"] = label_row.get_text()
            item["target"] = target_row.get_text()
            item["icon"] = icon_row.get_text()
            self._save(); self._refresh_slices()

        for row in (label_row, target_row, icon_row):
            row.connect("changed", apply)
        dialog.connect("close-request", lambda _d: (apply(), False)[1])

        if shutil.which("rofi"):
            pick_button = Gtk.Button(label="Pick app with Rofi…",
                                     margin_top=6, margin_start=12,
                                     margin_end=12, margin_bottom=12,
                                     halign=Gtk.Align.START)

            def on_pick(_button) -> None:
                def picked(entry: dict | None) -> None:
                    if entry is None:
                        return
                    item["type"] = "desktop"
                    label_row.set_text(entry["label"])
                    target_row.set_text(entry["target"])
                    icon_row.set_text(entry["icon"])
                    apply()
                self._pick_app_via_rofi(picked)

            pick_button.connect("clicked", on_pick)
            box.append(pick_button)

        dialog.set_content(box)
        dialog.present()

    # -- the other pages ---------------------------------------------------
    def _appearance_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Appearance", icon_name="applications-graphics-symbolic")

        size = Adw.PreferencesGroup(title="Geometry")
        for key, title, lower, upper in (
            ("menu_radius", "Wheel radius", 60, 500),
            ("icon_size", "Icon size", 24, 160),
            ("activation_threshold", "Dead zone", 20, 300),
            ("app_spacing", "Spacing between icons", 0, 80),
        ):
            row = Adw.SpinRow.new_with_range(lower, upper, 1)
            row.set_title(title)
            self._bind_spin(row, "appearance", key)
            size.add(row)

        reach = Adw.SpinRow.new_with_range(0, 800, 10)
        reach.set_title("Aim reach")
        reach.set_subtitle("How far past the ring aiming still counts. "
                           "Move beyond it and nothing is selected. 0 = unlimited")
        self._bind_spin(reach, "appearance", "aim_reach")
        size.add(reach)
        page.add(size)

        look = Adw.PreferencesGroup(title="Look")
        style = Adw.ComboRow(title="Backdrop",
                             subtitle="Dim the whole screen, or only around the wheel",
                             model=_combo(["Around the wheel", "Whole screen"]))
        self._bind_combo(style, "appearance", "background_style", ["circle", "fullscreen"])
        look.add(style)

        for key, title in (("backdrop_opacity", "Backdrop strength"),
                           ("menu_opacity", "Hub opacity")):
            row = Adw.ActionRow(title=title)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
            scale.set_size_request(220, -1)
            scale.set_value(float(self.cfg["appearance"][key]))
            scale.set_valign(Gtk.Align.CENTER)
            scale.connect("value-changed",
                          lambda s, k=key: (self.cfg["appearance"].__setitem__(k, round(s.get_value(), 2)),
                                            self._save()))
            row.add_suffix(scale)
            look.add(row)

        colour_row = Adw.ActionRow(title="Highlight colour")
        colour = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(), valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        rgba.parse(self.cfg["appearance"]["hover_color"])
        colour.set_rgba(rgba)
        colour.connect("notify::rgba", self._on_colour)
        colour_row.add_suffix(colour)
        look.add(colour_row)

        labels = Adw.SwitchRow(title="Show labels")
        self._bind_switch(labels, "appearance", "show_labels")
        look.add(labels)

        hover_only = Adw.SwitchRow(title="Only label the slice you are aiming at")
        self._bind_switch(hover_only, "appearance", "labels_on_hover_only")
        look.add(hover_only)
        page.add(look)
        return page

    def _on_colour(self, button, _param) -> None:
        rgba = button.get_rgba()
        self.cfg["appearance"]["hover_color"] = "#%02X%02X%02X" % (
            int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))
        self._save()

    def _trigger_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Trigger", icon_name="input-mouse-symbolic")

        group = Adw.PreferencesGroup(title="Mouse")
        mode = Adw.ComboRow(
            title="Mode",
            subtitle="Hold keeps middle-click paste working; click does not",
            model=_combo(["Hold and release", "Click to open, click to launch"]))
        self._bind_combo(mode, "general", "trigger_mode", ["hold", "click"])
        group.add(mode)

        button = Adw.ComboRow(title="Button",
                              model=_combo(["Middle", "Side", "Extra", "None (keybind only)"]))
        self._bind_combo(button, "general", "trigger_button",
                         ["middle", "side", "extra", "none"])
        group.add(button)

        hold = Adw.SpinRow.new_with_range(60, 800, 10)
        hold.set_title("Hold time")
        hold.set_subtitle("Milliseconds before a press becomes a gesture")
        self._bind_spin(hold, "general", "hold_ms")
        group.add(hold)

        desktop_only = Adw.SwitchRow(
            title="Only on the desktop",
            subtitle="Over a window, the button does what it normally does — "
                     "autoscroll, paste, close tab")
        self._bind_switch(desktop_only, "general", "desktop_only")
        group.add(desktop_only)

        grab = Adw.SwitchRow(
            title="Grab the mouse",
            subtitle="Required for aiming. Off means the wheel opens but cannot be aimed")
        self._bind_switch(grab, "general", "grab_mouse")
        group.add(grab)
        page.add(group)

        aiming = Adw.PreferencesGroup(title="Aiming")
        selection = Adw.ComboRow(
            title="Targeting",
            subtitle="Angle picks by direction from anywhere; cursor needs the pointer on the icon",
            model=_combo(["By angle", "By cursor"]))
        self._bind_combo(selection, "general", "selection_mode", ["angle", "cursor"])
        aiming.add(selection)
        page.add(aiming)

        centre = Adw.PreferencesGroup(title="Centre")
        action = Adw.ComboRow(title="Releasing in the middle",
                              model=_combo(["Opens the workspace picker",
                                            "Cancels", "Launches a target"]))
        self._bind_combo(action, "center", "action", ["picker", "none", "launch"])
        centre.add(action)
        target = Adw.EntryRow(title="Centre target")
        self._bind_entry(target, "center", "target")
        centre.add(target)
        label = Adw.EntryRow(title="Centre label")
        self._bind_entry(label, "center", "label")
        centre.add(label)
        page.add(centre)
        return page

    def _hud_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Overlay", icon_name="preferences-system-time-symbolic")
        group = Adw.PreferencesGroup(
            title="Heads-up display",
            description="Shown on the backdrop while the wheel is open")

        clock = Adw.SwitchRow(title="Clock")
        self._bind_switch(clock, "hud", "show_clock")
        group.add(clock)

        fmt = Adw.SwitchRow(title="24-hour clock")
        self._bind_switch(fmt, "hud", "clock_24h")
        group.add(fmt)

        position = Adw.ComboRow(title="Position", model=_combo(
            ["Top left", "Top centre", "Top right",
             "Bottom left", "Bottom centre", "Bottom right"]))
        self._bind_combo(position, "hud", "clock_position",
                         ["top-left", "top-center", "top-right",
                          "bottom-left", "bottom-center", "bottom-right"])
        group.add(position)

        battery = Adw.SwitchRow(title="Battery")
        self._bind_switch(battery, "hud", "show_battery")
        group.add(battery)

        weather = Adw.SwitchRow(title="Weather", subtitle="Fetched from wttr.in every 15 minutes")
        self._bind_switch(weather, "hud", "show_weather")
        group.add(weather)

        location = Adw.EntryRow(title="Weather location")
        self._bind_entry(location, "hud", "weather_location")
        group.add(location)
        page.add(group)
        return page

    def _advanced_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Advanced", icon_name="applications-system-symbolic")

        game = Adw.PreferencesGroup(
            title="Game mode", description="Suppress the wheel so it cannot interrupt")
        enabled = Adw.SwitchRow(title="Enabled")
        self._bind_switch(enabled, "game_mode", "enabled")
        game.add(enabled)
        auto = Adw.SwitchRow(title="While anything is fullscreen")
        self._bind_switch(auto, "game_mode", "auto_detect_fullscreen")
        game.add(auto)
        blocked = Adw.EntryRow(title="Blocked window classes (comma separated)")
        blocked.set_text(", ".join(self.cfg["game_mode"]["blocked_classes"]))
        blocked.connect("changed", lambda r: (
            self.cfg["game_mode"].__setitem__(
                "blocked_classes",
                [c.strip() for c in r.get_text().split(",") if c.strip()]),
            self._save()))
        game.add(blocked)
        page.add(game)

        backup = Adw.PreferencesGroup(title="Backup")
        export_row = Adw.ActionRow(title="Export configuration",
                                   subtitle=str(config.CONFIG_PATH))
        export_button = Gtk.Button(label="Export…", valign=Gtk.Align.CENTER)
        export_button.connect("clicked", self._on_export)
        export_row.add_suffix(export_button)
        backup.add(export_row)

        import_row = Adw.ActionRow(title="Import configuration",
                                   subtitle="Replaces every workspace and setting")
        import_button = Gtk.Button(label="Import…", valign=Gtk.Align.CENTER)
        import_button.connect("clicked", self._on_import)
        import_row.add_suffix(import_button)
        backup.add(import_row)
        page.add(backup)
        return page

    def _on_export(self, _button) -> None:
        dialog = Gtk.FileDialog(initial_name="rota-config.toml")
        dialog.save(self, None, self._export_done)

    def _export_done(self, dialog, result) -> None:
        try:
            path = dialog.save_finish(result).get_path()
        except GLib.Error:
            return
        config.save(self.cfg)
        Gio.File.new_for_path(str(config.CONFIG_PATH)).copy(
            Gio.File.new_for_path(path), Gio.FileCopyFlags.OVERWRITE, None, None, None)

    def _on_import(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.open(self, None, self._import_done)

    def _import_done(self, dialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        Gio.File.new_for_path(path).copy(
            Gio.File.new_for_path(str(config.CONFIG_PATH)),
            Gio.FileCopyFlags.OVERWRITE, None, None, None)
        self.cfg = config.load()
        self._save_now()
        toast = Adw.Toast(title="Configuration imported — reopen settings to see it")
        self.add_toast(toast)


class SettingsApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.rota.Settings")

    def do_activate(self) -> None:
        SettingsWindow(self).present()


def run() -> int:
    return SettingsApp().run([])
