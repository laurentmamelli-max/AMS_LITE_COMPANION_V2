#!/bin/bash
# Install the verified, pinned PrintGuard source runtime used by Companion.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HOME/Library/Application Support/AMS Lite Companion V2/printguard-engine-v2.3.7"
DATA="$HOME/Library/Application Support/AMS Lite Companion V2/printguard-data"
AGENT="$HOME/Library/LaunchAgents/fr.laurentmamelli.printguard.local.plist"
LABEL="fr.laurentmamelli.printguard.local"
TAG="v2.3.7"

if ! command -v uv >/dev/null 2>&1; then
  echo "L’outil uv est requis. Installe-le avec : brew install uv"
  exit 1
fi

mkdir -p "$DATA" "$(dirname "$AGENT")"
if [ ! -e "$TARGET" ]; then
  git clone --depth 1 --branch "$TAG" https://github.com/oliverbravery/PrintGuard.git "$TARGET"
elif [ ! -d "$TARGET/.git" ]; then
  echo "Le dossier cible existe mais n’est pas un clone PrintGuard : $TARGET"
  exit 1
fi

cd "$TARGET"
test "$(git rev-parse HEAD)" = "8d371d38a59ca19aafe714168620f48c727126b3"
uv sync --locked --no-dev
cp "$ROOT/macos/$LABEL.plist" "$AGENT"
launchctl unload "$AGENT" 2>/dev/null || true
launchctl load -w "$AGENT"
echo "PrintGuard est prêt sur http://127.0.0.1:8000 (alerte seule via Companion)."
