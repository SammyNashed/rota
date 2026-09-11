"""Favicons for URL slices, cached on disk.

Fetched straight from the site rather than through a favicon proxy, so a URL you
configured is only ever requested from the host it points at.
"""
from __future__ import annotations

import hashlib
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "rota" / "favicons"
_in_flight: set[str] = set()
_lock = threading.Lock()


def _cache_path(host: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(host.encode()).hexdigest()[:16] + ".ico")


def cached_path(url: str) -> str | None:
    """Path to a cached favicon, kicking off a fetch if there is not one yet."""
    host = urllib.parse.urlparse(url if "://" in url else f"https://{url}").netloc
    if not host:
        return None
    path = _cache_path(host)
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    _fetch_async(host, path)
    return None


def _fetch_async(host: str, path: Path) -> None:
    with _lock:
        if host in _in_flight:
            return
        _in_flight.add(host)
    threading.Thread(target=_fetch, args=(host, path), daemon=True).start()


def _fetch(host: str, path: Path) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            f"https://{host}/favicon.ico",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) rota"})
        with urllib.request.urlopen(request, timeout=6) as response:
            body = response.read(200_000)
        if body[:2] not in (b"\x00\x00", b"\x89P", b"GI", b"\xff\xd8", b"<s", b"<?"):
            return
        # Write through a temp file: a half-downloaded icon in the cache would be
        # served forever, since presence is what marks it done.
        tmp = path.with_suffix(".part")
        tmp.write_bytes(body)
        os.replace(tmp, path)
    except Exception:
        pass
    finally:
        with _lock:
            _in_flight.discard(host)
