#!/bin/bash
set -euo pipefail

APP_DATA_DIR="$HOME/Library/Application Support/AMS Lite Companion V2"
RUNTIME_DIR="$APP_DATA_DIR/vision-runtime"
PYTHON_BIN="${1:-/usr/bin/python3}"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python 3 introuvable : $PYTHON_BIN"
  exit 1
fi
mkdir -p "$RUNTIME_DIR"
echo "Installation locale du moteur OpenCV LINEMOD…"
"$PYTHON_BIN" -m pip install --upgrade --target "$RUNTIME_DIR" opencv-contrib-python
echo
echo "Moteur Vision installé dans : $RUNTIME_DIR"
echo "Interpréteur Vision : $PYTHON_BIN"
echo "Relance AMS Lite Companion puis ouvre une capture de l’impression en cours."
