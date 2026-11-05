#!/usr/bin/env bash
# Install clickable Olith launchers in the app menu and on the desktop.
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$APPS" "$DESKTOP_DIR"

# Rewrite the absolute paths for wherever this checkout actually lives.
gen() {
  sed -e "s|^Exec=.*|Exec=${APP_DIR}/olith.sh|" \
      -e "s|^Path=.*|Path=${APP_DIR}|" \
      -e "s|^Icon=.*|Icon=${APP_DIR}/static/assets/olith.png|" \
      Olith.desktop > "$1"
}

chmod +x olith.sh

gen "$APPS/olith.desktop"
chmod 644 "$APPS/olith.desktop"

gen "$DESKTOP_DIR/Olith.desktop"
chmod 755 "$DESKTOP_DIR/Olith.desktop"

# Mark the launcher trusted for GNOME. Prefer system gio because other
# installs may lack GVFS metadata support.
for GIO in /usr/bin/gio "$(command -v gio 2>/dev/null || true)"; do
  [ -x "$GIO" ] || continue
  if "$GIO" set "$DESKTOP_DIR/Olith.desktop" metadata::trusted true 2>/dev/null; then
    break
  fi
done
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" 2>/dev/null || true
fi

echo "Installed:"
echo "  app menu : $APPS/olith.desktop"
echo "  desktop  : $DESKTOP_DIR/Olith.desktop"
