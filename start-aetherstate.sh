#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: installing into a private environment - takes a minute or two..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .
fi
mkdir -p aetherstate-data
[ -f aetherstate-data/config.toml ] || cp config.example.toml aetherstate-data/config.toml
echo "Console: http://127.0.0.1:9130/aether/console"
exec .venv/bin/python -m aetherstate
