#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "=========================================="
echo "  AetherState + SillyTavern quick install"
echo "=========================================="
echo

st_dir=""
install_only=false

for argument in "$@"; do
  case "$argument" in
    --install-only) install_only=true ;;
    *)
      if [ -n "$st_dir" ]; then
        echo "Only one SillyTavern folder may be supplied." >&2
        exit 2
      fi
      st_dir="$argument"
      ;;
  esac
done

is_sillytavern() {
  [ -d "$1/data/default-user" ]
}

if [ -z "$st_dir" ] && [ -n "${SILLYTAVERN_DIR:-}" ] && is_sillytavern "$SILLYTAVERN_DIR"; then
  st_dir="$SILLYTAVERN_DIR"
fi

if [ -z "$st_dir" ]; then
  for candidate in \
    "$PWD/SillyTavern" \
    "$PWD/../SillyTavern" \
    "$HOME/SillyTavern" \
    "$HOME/Documents/SillyTavern" \
    "$HOME/.local/share/SillyTavern"
  do
    if is_sillytavern "$candidate"; then
      st_dir="$candidate"
      break
    fi
  done
fi

if [ -z "$st_dir" ] && [ -t 0 ]; then
  echo "SillyTavern was not found automatically."
  read -r -p "Paste its folder path, or press Enter to install only AetherState: " st_dir
fi

if [ -n "$st_dir" ]; then
  if ! is_sillytavern "$st_dir"; then
    echo "That folder is not a ready SillyTavern install: $st_dir" >&2
    echo "Start SillyTavern once so data/default-user exists, then run this installer again." >&2
    exit 1
  fi
  st_dir="$(cd "$st_dir" && pwd -P)"
  destination="$st_dir/data/default-user/extensions/AetherState"
  mkdir -p "$destination"
  cp -R st-extension/. "$destination/"
  for file in manifest.json index.js style.css; do
    cmp -s "st-extension/$file" "$destination/$file" || {
      echo "Companion verification failed for $file." >&2
      exit 1
    }
  done
  echo "AetherState Companion installed in SillyTavern."
else
  echo "Companion install skipped. Copy st-extension later if needed."
fi

if $install_only; then
  echo "Install-only verification complete."
  exit 0
fi

echo "Starting AetherState setup and Console..."
exec ./start-aetherstate.sh
