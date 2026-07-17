#!/usr/bin/env bash
# AetherState -- local grounded fact-checker / NLI contradiction setup (Linux / macOS).
# Creates a venv, lets you PICK the checker model, installs the right deps, and starts the shim
# on 127.0.0.1:8199. The choice is saved to selected-backend.txt.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[nli] creating virtual environment..."
  python3 -m venv .venv
fi
PY=.venv/bin/python
"$PY" -m pip install --upgrade pip >/dev/null

echo
echo "Pick the grounding / contradiction model:"
echo "  1) FactCG-DeBERTa-L    (0.4B, MIT)   [recommended - default]"
echo "  2) MiniCheck-FT5       (770M, MIT)"
echo "  3) roberta-large-mnli  (legacy 3-way NLI)"
read -r -p "Enter 1, 2 or 3 (default 1): " CH
case "$CH" in
  2) BACKEND=minicheck ;;
  3) BACKEND=nli ;;
  *) BACKEND=factcg ;;
esac

echo "[nli] selected backend: $BACKEND"
echo "[nli] installing dependencies (CPU torch; first run downloads the checker)..."
"$PY" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
case "$BACKEND" in
  minicheck) "$PY" -m pip install "minicheck @ git+https://github.com/Liyan06/MiniCheck.git@main" "accelerate>=0.26.0" sentencepiece truststore ;;
  nli)       "$PY" -m pip install transformers truststore ;;
  *)         "$PY" -m pip install transformers sentencepiece truststore ;;
esac

echo "$BACKEND" > selected-backend.txt

echo "[nli] starting the shim on http://127.0.0.1:8199 (backend=$BACKEND)"
echo "[nli] the FIRST start downloads the model to your HuggingFace cache."
NLI_BACKEND="$BACKEND" "$PY" server.py
