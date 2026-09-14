"""Global middle-button trigger.

Rovyl's own notes explain why a poll is not good enough on Windows: merely
*observing* the button leaves the event to reach the window underneath, which
starts autoscroll, so aiming drags the page behind the wheel. The same is true
here for a different reason. A wl_pointer button press begins an implicit grab
on the surface that received it, so while the middle button is held, the app
underneath keeps the pointer and our overlay would never see a motion event.

So whatever detects the button must also swallow it. On Linux that is EVIOCGRAB
on the physical device plus a uinput clone that everything else is replayed
through. A short, stationary press is not a gesture, so it is synthesised back
out as a real middle click and middle-click paste keeps working.
"""
from __future__ import annotations

import glob
import os
import selectors
import threading
import time

import evdev
from evdev import ecodes

_BUTTONS = {
    "middle": ecodes.BTN_MIDDLE,
    "side": ecodes.BTN_SIDE,
    "extra": ecodes.BTN_EXTRA,
}

IDLE, PENDING, OPEN = 0, 1, 2
# The press landed somewhere the trigger does not apply (over a window, say):
# it is forwarded untouched and its release must follow it out.
PASSTHROUGH = 3

RESCAN_SECONDS = 2.0

# The virtual device is declared generically rather than cloned from whichever
# mouse happened to be plugged in at startup. That is what lets a mouse be
# unplugged and replugged without rebuilding it — and a uinput device cannot
# gain capabilities after creation.
VIRTUAL_NAME = "rota-virtual-pointer"

VIRTUAL_CAPS = {
    ecodes.EV_KEY: [
        ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE,
        ecodes.BTN_SIDE, ecodes.BTN_EXTRA, ecodes.BTN_FORWARD,
        ecodes.BTN_BACK, ecodes.BTN_TASK,
    ],
    ecodes.EV_REL: [
        ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL,
        ecodes.REL_WHEEL_HI_RES, ecodes.REL_HWHEEL_HI_RES,
    ],
}


def find_mice(button_code: int = ecodes.BTN_MIDDLE,
              denied: set[str] | None = None) -> list[evdev.InputDevice]:
    """Real mice carrying the trigger button, and nothing else.

    Three filters, each earned by a device on this machine:

    - REL_X excludes touchpads. Grabbing one and replaying its packets would
      bypass libinput's gesture and palm handling.
    - No KEY_A excludes keyboards. The ASUS N-KEY device reports REL_X *and*
      281 key codes; grabbing it would swallow every keystroke typed while the
      daemon runs, which is the worst failure this program could have.
    - The trigger button itself excludes nodes that could never fire it, such as
      a touchpad's two-button legacy pointer node.
    - Our own virtual pointer is excluded, or the daemon grabs the device it
      replays through and every forwarded event comes straight back in. The name
      match also catches a device left behind by a crashed instance.

    Deliberately does *not* use evdev.list_devices(): it silently drops any
    node the process cannot currently write to, which is exactly what a mouse
    with a desynced seat ACL looks like (systemd/logind updating mid-session
    has been seen to leave a device's ACL mask empty — see README). Globbing
    ourselves and opening every node means a permission failure surfaces as a
    logged warning instead of the mouse just vanishing with no explanation.
    """
    found = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            device = evdev.InputDevice(path)
        except PermissionError:
            if denied is not None and path not in denied:
                denied.add(path)
                print(f"[rota] permission denied opening {path}; if this is "
                      "your mouse, its seat ACL is likely desynced (common "
                      "after a systemd/udev update mid-session) — unplug and "
                      "replug it, or reboot, to force logind to regrant it")
            continue
        except OSError:
            continue
        if denied is not None:
            denied.discard(path)
        caps = device.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        rels = caps.get(ecodes.EV_REL, [])
        if device.name == VIRTUAL_NAME:
            device.close()
            continue
        is_mouse = ecodes.BTN_LEFT in keys and ecodes.REL_X in rels
        is_keyboard = ecodes.KEY_A in keys
        has_trigger = button_code in keys
        if is_mouse and has_trigger and not is_keyboard:
            found.append(device)
        else:
            device.close()
    return found


class MouseTrigger(threading.Thread):
    """Watches the trigger button and calls back on open / commit / cancel.

    Callbacks fire on this thread — the wheel marshals them onto the GTK loop.
    """

    daemon = True

    def __init__(self, hold_ms: int, button: str, on_open, on_commit,
                 grab: bool = True, mode: str = "hold", on_toggle=None,
                 should_trigger=None):
        super().__init__(name="rota-trigger")
        # Asked on every press. False hands the button to the app under the
        # cursor for the whole press — autoscroll, paste, tab close all intact.
        self.should_trigger = should_trigger or (lambda: True)
        self.hold_seconds = max(hold_ms, 0) / 1000.0
        self.mode = mode
        self.on_toggle = on_toggle or on_open
        self.button_code = _BUTTONS.get(button, ecodes.BTN_MIDDLE)
        self.on_open = on_open
        self.on_commit = on_commit
        self.want_grab = grab
        self.devices: list[evdev.InputDevice] = []
        self.ui: evdev.UInput | None = None
        self.grabbed = False
        self.state = IDLE
        self._pressed_at = 0.0
        self._stop = threading.Event()
        self._sel = selectors.DefaultSelector()
        self._last_scan = 0.0
        self._fingerprint: frozenset = frozenset()
        self._denied: set[str] = set()

    # -- lifecycle ---------------------------------------------------------
    def setup(self) -> str:
        """Returns a human-readable description of the mode we ended up in."""
        if self.want_grab:
            try:
                self.ui = evdev.UInput(VIRTUAL_CAPS, name=VIRTUAL_NAME)
            except Exception as exc:
                self.ui = None
                return (f"uinput unavailable ({exc}); observing only — "
                        "install the udev rule to enable button swallowing")
        attached = self._rescan()
        if not attached:
            return ("no mouse with the trigger button attached yet; "
                    "will pick one up when it appears")
        mode = "grabbing" if self.grabbed else "observing"
        return f"{mode} {attached} device(s)" + (" via uinput" if self.ui else "")

    @staticmethod
    def _device_names() -> frozenset:
        """A listing of /dev/input — tens of microseconds, no device is opened."""
        try:
            return frozenset(os.listdir("/dev/input"))
        except OSError:
            return frozenset()

    def _needs_rescan(self) -> bool:
        """Whether the expensive probe is worth running at all.

        Opening all 24 event nodes to read their capabilities costs ~180ms, and
        it happens on the thread that forwards pointer events — so doing it on a
        timer froze the cursor for a fifth of a second every two seconds. The
        cheap directory listing tells us whether anything could possibly have
        changed.

        The exception is having no mouse at all: a device can become usable
        without appearing or disappearing (its ACL is fixed, say), and with
        nothing attached there is no event stream to stutter.
        """
        names = self._device_names()
        if names != self._fingerprint:
            self._fingerprint = names
            return True
        return not self.devices

    def _rescan(self) -> int:
        """Attach newly appeared mice, drop ones that went away."""
        self._last_scan = time.monotonic()
        self._fingerprint = self._device_names()
        known = {device.path for device in self.devices}
        present = set()

        for device in find_mice(self.button_code, denied=self._denied):
            present.add(device.path)
            if device.path in known:
                device.close()          # already attached; this is a duplicate handle
                continue
            name = device.name
            if self.ui and self.want_grab:
                try:
                    device.grab()
                    self.grabbed = True
                except OSError as exc:
                    print(f"[rota] cannot grab {name}: {exc}")
                    device.close()
                    continue
            self.devices.append(device)
            self._sel.register(device, selectors.EVENT_READ)
            print(f"[rota] attached {name}")

        for device in list(self.devices):
            if device.path in present:
                continue
            name = device.name if device.fd != -1 else device.path
            try:
                self._sel.unregister(device)
            except (KeyError, ValueError):
                pass
            try:
                device.close()
            except OSError:
                pass
            self.devices.remove(device)
            print(f"[rota] detached {name}")

        return len(self.devices)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        if self.grabbed:
            for device in self.devices:
                try:
                    device.ungrab()
                except OSError:
                    pass
        if self.ui:
            self.ui.close()
        for device in self.devices:
            try:
                device.close()
            except OSError:
                pass

    # -- event plumbing ----------------------------------------------------
    def _forward(self, event) -> None:
        if self.ui:
            self.ui.write(event.type, event.code, event.value)

    def _syn(self) -> None:
        if self.ui:
            self.ui.syn()

    def _synth_click(self) -> None:
        """Replay the middle click we swallowed, so the app underneath gets it."""
        if not self.ui:
            return
        self.ui.write(ecodes.EV_KEY, self.button_code, 1)
        self.ui.syn()
        self.ui.write(ecodes.EV_KEY, self.button_code, 0)
        self.ui.syn()

    def run(self) -> None:
        while not self._stop.is_set():
            # While a press is pending, the loop must wake on its own to promote
            # the hold into a gesture even if the mouse never moves again.
            timeout = 0.05
            if self.state == PENDING:
                remaining = self.hold_seconds - (time.monotonic() - self._pressed_at)
                # Floor it. A zero timeout turns select() into a spin, and if the
                # state ever failed to advance this loop would eat a core.
                timeout = min(max(remaining, 0.001), 0.05)

            for key, _ in self._sel.select(timeout=timeout):
                try:
                    for event in key.fileobj.read():
                        self._handle(event)
                except OSError:
                    # The device went away mid-read; the next rescan drops it.
                    continue

            if self.state == PENDING and \
                    time.monotonic() - self._pressed_at >= self.hold_seconds:
                self.state = OPEN
                self.on_open()

            # Never rescan mid-gesture: attaching a device would re-register the
            # selector under a press this loop is still tracking.
            if self.state == IDLE and \
                    time.monotonic() - self._last_scan >= RESCAN_SECONDS and \
                    self._needs_rescan():
                self._rescan()

        self._sel.close()

    def _handle(self, event) -> None:
        if event.type == ecodes.EV_KEY and event.code == self.button_code:
            if event.value == 1 and self.state == IDLE and not self.should_trigger():
                self.state = PASSTHROUGH
            if self.state == PASSTHROUGH:
                # Forward the real press, repeats and release as they happen,
                # with no hold delay — the app must see a genuinely held button
                # or browser autoscroll never starts.
                self._forward(event)
                if event.value == 0:
                    self.state = IDLE
                return
            if self.mode == "click":
                # The button is the trigger outright, so it is never replayed —
                # in this mode you trade middle-click paste for a wheel that
                # stays up after a tap.
                if event.value == 1:
                    self.on_toggle()
                self.state = IDLE
                return
            if event.value == 1:
                self._pressed_at = time.monotonic()
                self.state = PENDING
            elif event.value == 0:
                if self.state == OPEN:
                    self.on_commit()
                elif self.state == PENDING:
                    # Too short to be a gesture: it was an ordinary middle click.
                    self._synth_click()
                self.state = IDLE
            return  # swallowed in every case; never forwarded raw

        self._forward(event)
        if event.type == ecodes.EV_SYN:
            self._syn()
