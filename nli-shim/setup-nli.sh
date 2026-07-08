#!/usr/bin/env bash
# AetherState -- local NLI contradiction model setup (Linux/macOS).
# Creates a venv, installs CPU torch + transformers, and starts the shim on 127.0.0.1:8199.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "[nli] creating virtual environment..."
  python3 -m venv .venv
fi
echo "[nli] installing dependencies (first run downloads torch + transformers)..."
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
./.venv/bin/python -m pip install "transformers>=4.40"
echo
echo "[nli] starting the shim on http://127.0.0.1:8199"
echo "[nli] (the FIRST start downloads the model, ~1.4 GB, to your HuggingFace cache)"
exec ./.venv/bin/python server.py
