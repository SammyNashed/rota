#!/usr/bin/env bash
# Rota launcher. Kept as a script so the systemd unit and a Hyprland keybind
# both go through one place.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
# -u so the daemon's output reaches the journal as it happens, not when a
# block buffer eventually fills.
exec python3 -u -m rota "$@"
