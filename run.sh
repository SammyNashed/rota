#!/usr/bin/env bash
# Rota launcher. Kept as a script so the systemd unit and a Hyprland keybind
# both go through one place.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
# GTK4's GL renderer (the session-wide default, set globally for every app)
# paints Rota's fullscreen layer-shell overlay solid black on this machine's
# Optimus/iGPU setup as of mesa 26.2 + nvidia 615 (2026-09-14) — same class of
# bug as other GTK4 apps here hit on the same combo. cairo is slower but this
# overlay is small and short-lived, so it's not worth chasing the GL bug.
# Scoped to this process only; the global GSK_RENDERER=gl in the Hyprland
# config is left alone for every other app.
export GSK_RENDERER=cairo
# -u so the daemon's output reaches the journal as it happens, not when a
# block buffer eventually fills.
exec python3 -u -m rota "$@"
