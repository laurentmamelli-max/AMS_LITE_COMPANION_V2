#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 || true)"

if [ -z "$PYTHON" ]; then
  osascript -e 'display alert "Python 3 est nécessaire" message "Installez Python 3 avec : brew install python"'
  exit 1
fi

# Le nom varie légèrement selon le paquet officiel installé.
if ! open -a "BambuStudio" 2>/dev/null; then
  if ! open -a "Bambu Studio" 2>/dev/null; then
    osascript -e 'display alert "Bambu Studio introuvable" message "Installez l’application officielle dans le dossier Applications."'
  fi
fi

# Companion reste volontairement au premier plan. La fermeture est propre avec
# Ctrl+C et aucun processus ne continue ensuite en arrière-plan.
exec "$PYTHON" "$DIR/ams_companion.py"
