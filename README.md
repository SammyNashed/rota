# Rota

A native Wayland/Hyprland reimplementation of [Rovyl](https://github.com/HenryCauan/rovyl)
(GPL-3.0, by HenryCa) — a radial launcher. Hold the middle mouse button on your desktop,
a wheel blooms under the cursor, flick toward a slice, release to launch.

> **Rota is an independent project, not affiliated with Rovyl or its author.**
> It is rewritten from scratch — Rovyl is Electron + React for Windows, Rota is
> Python + GTK4 for Wayland — but its design, behaviour and much of its reasoning
> come from [Rovyl](https://github.com/HenryCauan/rovyl) by HenryCa, so it is
> released under the same licence, GPL-3.0-or-later.

The original is Electron + React and welded to Win32: a `WH_MOUSE_LL` hook in
PowerShell for the trigger, `IShellItemImageFactory` for icons, Start Menu `.lnk`
discovery, and a lot of DWM anti-flicker choreography. None of that ports. This
is a rewrite of the *behaviour* in Python + GTK4 + layer-shell, keeping the parts
of the original that were hard-won.

## What is carried over

- **The aiming geometry.** Radius 140, icon 64, dead zone 60, `angle` and
  `cursor` targeting modes — the same numbers and the same trigonometry.
- **Confirmation resolves from the live pointer**, never from render state, so
  releasing mid-flight cannot confirm a slice the pointer already left.
- **Highlight and confirmation share one function** (`Wheel.resolve_aim`). Two
  copies can diverge, and lighting one icon while opening another is the worst
  defect a launcher can have.
- **A short press is not a gesture.** It is replayed as a real middle click, so
  middle-click paste and close-tab keep working.

## What is different, and why

| Original | Here |
| --- | --- |
| `WH_MOUSE_LL` hook swallows the button | `EVIOCGRAB` + a uinput clone |
| Foreground stealing, always-on-top races, paint handshake | layer-shell: the compositor does not present until we have painted |
| Start Menu `.lnk` scan | `Gio.AppInfo` over `.desktop` files |
| Icon extraction from PE resources, white-halo scoring | the icon theme, which already resolves names to real SVG assets |
| `active-win` for Game Mode | `hyprctl activewindow` |

The reason a grab is needed at all is not the Windows one. On Wayland a pointer
button press begins an *implicit grab* on the surface that received it, so while
the middle button is held the app underneath keeps the pointer and the overlay
would never see a motion event. Whatever detects the button must also swallow it.

## Requirements

**Hyprland** — the cursor position and the "is this the desktop?" check come
from its IPC — plus Python 3.11+ and:

```sh
sudo pacman -S python-gobject python-cairo python-evdev gtk4 gtk4-layer-shell libadwaita
```

## Install

```sh
git clone https://github.com/SammyNashed/rota ~/rota
mkdir -p ~/.local/bin && ln -sf ~/rota/run.sh ~/.local/bin/rota

# One-time: let the daemon replay the pointer events it grabs.
# Trigger only /dev/uinput. Re-triggering the whole input subsystem reapplies
# logind's uaccess ACLs on USB input devices and can leave them with an empty
# mask — the ACL is still listed, marked "#effective:---", and the device
# becomes unreadable until it is replugged.
# Both halves are needed. uinput is a module on Arch and /dev/uinput is a static
# node, so it exists even unloaded and opening it fails with ENODEV — which
# reads like a permissions problem and is not one.
sudo cp ~/rota/packaging/uinput.conf /etc/modules-load.d/uinput.conf
sudo cp ~/rota/packaging/99-rota-uinput.rules /etc/udev/rules.d/
sudo modprobe uinput
sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=/dev/uinput

mkdir -p ~/.config/systemd/user ~/.local/share/applications
cp ~/rota/packaging/rota.service ~/.config/systemd/user/
cp ~/rota/packaging/rota-settings.desktop ~/.local/share/applications/
systemctl --user daemon-reload
systemctl --user enable --now rota.service
```

Without the udev rule the daemon still runs, but only in observe-only mode: it
cannot swallow the middle button, so aiming fights the window underneath.

## Use

- **Hold middle mouse on the desktop** (180 ms) → wheel opens. Aim, release.
  Over a window (or waybar) the button is passed straight to the app, held
  presses included, so browser autoscroll, paste and close-tab are untouched.
  Turn this off in Settings → Trigger → *Only on the desktop* to trigger anywhere.
- Moving the cursor well past the ring un-aims, and releasing there opens
  nothing — so overshooting a slice is a way to change your mind.
- `rota toggle` opens and closes the wheel from a script or a keybind of your
  own. Nothing is bound by default; see `packaging/hyprland.conf.snippet`.
- **Right-click**, **Escape** or **Backspace** step back: out of a sub-wheel,
  or from a workspace up to the list of workspaces. **1–9** jump straight to one.
- **Releasing over the hub** cancels. **Clicking** the hub opens the workspace
  list, or launches a target of its own if you set one.
- Slice types: installed app, raw command, website (favicon fetched and cached),
  folder, terminal command, and **sub-wheel** — nested as deep as you like.
- The backdrop carries an optional clock, battery and weather HUD.
- Game mode suppresses the wheel while anything is fullscreen, or while one of
  the window classes you name is focused.

### Settings

```sh
rota settings          # or launch "Rota" from your app menu
```

A libadwaita window: slice editor (add installed apps, websites, folders,
commands and nested sub-wheels; reorder and edit in place), appearance,
trigger, HUD and game mode. There is no Save button — changes are debounced,
written to `config.toml`, and the running daemon is told to reload, so the next
time you open the wheel it is already different.

### From the terminal

```sh
rota list                          # what is on each wheel
rota apps                          # every installed .desktop
rota add firefox --label Web       # app, url, folder or raw command
rota add ~/Projects --workspace 2
rota seed                          # re-detect and refill MAIN
```

Config is `~/.config/rota/config.toml`; stored values are spread *over* the
defaults, so a setting added after your file was written arrives as its default
rather than as nothing.

## Device selection

The daemon grabs only real mice carrying the trigger button. Three filters, each
earned by a real device: no touchpads (grabbing one bypasses
libinput's gesture and palm handling), nothing reporting `KEY_A` (the ASUS N-KEY
device reports `REL_X` *and* 281 key codes — grabbing it would swallow every
keystroke), and nothing that lacks the trigger button.

## Licence

Rota is free software under the GNU General Public License, version 3 or later —
see [LICENSE](LICENSE). Copyright © 2026 Sammy.

Based on [Rovyl](https://github.com/HenryCauan/rovyl) © HenryCa, GPL-3.0-or-later.
Modified in 2026: reimplemented for Linux/Wayland in Python and GTK4.
