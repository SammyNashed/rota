"""Clock, battery and weather, drawn onto the wheel's canvas.

Everything here reads from sysfs or a cache. Nothing blocks the paint: the
weather is fetched on a worker thread and the wheel draws whatever the cache
last held, including nothing at all.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_POWER_SUPPLY = Path("/sys/class/power_supply")
_WEATHER_TTL = 900.0   # 15 minutes; the wheel is not a weather station


def clock_text(use_24h: bool) -> str:
    return datetime.now().strftime("%H:%M" if use_24h else "%I:%M %p").lstrip("0")


def battery_text() -> str | None:
    """First real battery found, as '87%' or '87% ⚡'."""
    if not _POWER_SUPPLY.exists():
        return None
    for entry in sorted(_POWER_SUPPLY.iterdir()):
        try:
            if (entry / "type").read_text().strip() != "Battery":
                continue
            capacity = (entry / "capacity").read_text().strip()
            status = (entry / "status").read_text().strip()
        except OSError:
            continue
        return f"{capacity}% ⚡" if status == "Charging" else f"{capacity}%"
    return None


class Weather:
    """wttr.in, refreshed at most every 15 minutes, never on the paint thread."""

    def __init__(self) -> None:
        self._text: str | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._in_flight = False

    def text(self, location: str) -> str | None:
        if time.monotonic() - self._fetched_at > _WEATHER_TTL:
            self._refresh(location)
        with self._lock:
            return self._text

    def _refresh(self, location: str) -> None:
        with self._lock:
            if self._in_flight:
                return
            self._in_flight = True
            # Stamp the attempt, not the success: a failing network must not turn
            # into a fetch on every single frame.
            self._fetched_at = time.monotonic()
        threading.Thread(target=self._fetch, args=(location,), daemon=True).start()

    def _fetch(self, location: str) -> None:
        url = f"https://wttr.in/{urllib.parse.quote(location)}?format=%t+%C&m"
        result = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(request, timeout=6) as response:
                body = response.read().decode(errors="replace").strip()
            if body and "Unknown location" not in body and len(body) < 60:
                result = body.replace("+", "")
        except Exception:
            result = None
        with self._lock:
            if result:
                self._text = result
            self._in_flight = False


def lines(cfg: dict, weather: Weather) -> list[str]:
    """The HUD as one or two short strings, in draw order."""
    hud = cfg["hud"]
    parts = []
    if hud["show_clock"]:
        parts.append(clock_text(hud["clock_24h"]))
    if hud["show_battery"]:
        battery = battery_text()
        if battery:
            parts.append(battery)
    if hud["show_weather"] and hud["weather_location"]:
        forecast = weather.text(hud["weather_location"])
        if forecast:
            parts.append(forecast)
    return parts
