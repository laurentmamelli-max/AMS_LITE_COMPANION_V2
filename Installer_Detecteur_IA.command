#!/bin/bash
set -euo pipefail

APP_DATA_DIR="$HOME/Library/Application Support/AMS Lite Companion V2"
RUNTIME_DIR="$APP_DATA_DIR/detector-runtime"
MODEL_DIR="$RUNTIME_DIR/models"
PYTHON_BIN="${1:-/usr/bin/python3}"
MODEL_URL="https://huggingface.co/Yodazon/3DPrintFailureType/resolve/38d7ffcc6104aa28250e615492238ac90ba3ce80/CNNModelV0_2.pth"
MODEL_PATH="$MODEL_DIR/CNNModelV0_2.pth"
MODEL_SHA256="2ba203900ffb0b173d6f90fcf01a1fdcde0d6b96cefcf3b1c1c230a34ee9c705"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python 3 introuvable : $PYTHON_BIN"
  exit 1
fi
mkdir -p "$RUNTIME_DIR" "$MODEL_DIR"
echo "Installation du moteur IA local (PyTorch CPU)…"
"$PYTHON_BIN" -m pip install --upgrade --target "$RUNTIME_DIR" "torch==2.2.2" "numpy==1.26.4" "pillow==10.4.0"
echo "Téléchargement du modèle local MIT (environ 222 Mo)…"
curl --fail --location --retry 2 --output "$MODEL_PATH.part" "$MODEL_URL"
ACTUAL_SHA256="$(shasum -a 256 "$MODEL_PATH.part" | awk '{print $1}')"
if [ "$ACTUAL_SHA256" != "$MODEL_SHA256" ]; then
  rm -f "$MODEL_PATH.part"
  echo "Empreinte du modèle invalide"
  exit 1
fi
mv "$MODEL_PATH.part" "$MODEL_PATH"
echo "Détecteur IA prêt. Relance AMS Lite Companion."
