#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  osascript -e 'display alert "Python 3 est nécessaire" message "Installez Python 3 avec : brew install python"'
  exit 1
fi
exec "$PYTHON" ams_companion.py
