#!/bin/bash
set -euo pipefail

APP_DATA_DIR="$HOME/Library/Application Support/AMS Lite Companion V2"
RUNTIME_DIR="$APP_DATA_DIR/vision-runtime"

mkdir -p "$RUNTIME_DIR"
echo "Installation locale du moteur OpenCV LINEMOD…"
python3 -m pip install --upgrade --target "$RUNTIME_DIR" opencv-contrib-python
echo
echo "Moteur Vision installé dans : $RUNTIME_DIR"
echo "Relance AMS Lite Companion puis ouvre une capture de l’impression en cours."
