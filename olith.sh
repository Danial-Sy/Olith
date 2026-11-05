#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/olith"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/launch.log"

fail() {
  local msg="$1"
  printf '%s\n' "$msg" | tee -a "$LOG" >&2
  if [[ ! -t 1 ]] && command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Olith" --width=420 --text="$msg" || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "Olith" "$msg" || true
  fi
  exit 1
}

if [[ ! -x .venv/bin/python ]]; then
  fail "Olith is not set up yet.\n\nOpen a terminal and run:\n  cd ~/olith && ./setup.sh --skip-models"
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export GGML_CUDA_ENABLE_UNIFIED_MEMORY="${GGML_CUDA_ENABLE_UNIFIED_MEMORY:-1}"
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PYTHON_EXE CONDA_SHLVL 2>/dev/null || true

if [[ -t 1 ]]; then
  exec .venv/bin/python desktop.py
fi

# Start the window in a user service so it survives the desktop launcher
# exiting. The marker prevents recursion. Fall back to a direct launch
# when a user systemd session is unavailable.
if [[ "${OLITH_DESKTOP_DETACHED:-0}" != "1" ]] \
   && command -v systemd-run >/dev/null 2>&1 \
   && systemctl --user is-system-running >/dev/null 2>&1; then
  UNIT="olith-desktop-$$-$RANDOM"
  if systemd-run --user --quiet --collect --unit="$UNIT" \
      --property="WorkingDirectory=$PWD" \
      --setenv="DISPLAY=${DISPLAY:-}" \
      --setenv="WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}" \
      --setenv="XAUTHORITY=${XAUTHORITY:-}" \
      --setenv="XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}" \
      --setenv="DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}" \
      /usr/bin/env OLITH_DESKTOP_DETACHED=1 "$PWD/olith.sh"; then
    exit 0
  fi
fi

if ! .venv/bin/python desktop.py >>"$LOG" 2>&1; then
  fail "Olith failed to start.\n\nDetails: $LOG"
fi
