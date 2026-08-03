#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST="$ROOT/dist"
APP="$DIST/AMS Lite Companion V2.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$ROOT/macos/Info.plist")"
ARCHIVE="$DIST/AMS-Lite-Companion-V2-${VERSION}-macOS.zip"
BUILD="$DIST/.build"
SIGNING_IDENTITY="${CODESIGN_IDENTITY:--}"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Cette construction doit être lancée sur macOS."
  exit 1
fi

if ! command -v xcrun >/dev/null 2>&1; then
  echo "Les outils Apple sont nécessaires. Lancez : xcode-select --install"
  exit 1
fi

mkdir -p "$DIST"
rm -rf "$APP"
rm -rf "$BUILD"
mkdir -p "$MACOS" "$RESOURCES" "$BUILD"

xcrun swiftc \
  -O \
  -target arm64-apple-macosx11.0 \
  -framework AppKit \
  -framework Foundation \
  -framework WebKit \
  -o "$BUILD/AMS-Lite-Companion-V2-arm64" \
  "$ROOT/macos/AMSCompanionLauncher.swift"

xcrun swiftc \
  -O \
  -target x86_64-apple-macosx10.15 \
  -framework AppKit \
  -framework Foundation \
  -framework WebKit \
  -o "$BUILD/AMS-Lite-Companion-V2-x86_64" \
  "$ROOT/macos/AMSCompanionLauncher.swift"

xcrun lipo -create \
  "$BUILD/AMS-Lite-Companion-V2-arm64" \
  "$BUILD/AMS-Lite-Companion-V2-x86_64" \
  -output "$MACOS/AMS-Lite-Companion-V2"

cp "$ROOT/ams_companion.py" "$RESOURCES/ams_companion.py"
cp "$ROOT/plate_guardian.py" "$RESOURCES/plate_guardian.py"
cp "$ROOT/bambu_camera.py" "$RESOURCES/bambu_camera.py"
cp "$ROOT/gcode_mapper.py" "$RESOURCES/gcode_mapper.py"
cp "$ROOT/local_detector.py" "$RESOURCES/local_detector.py"
cp "$ROOT/vision_linemod.py" "$RESOURCES/vision_linemod.py"
cp "$ROOT/Installer_Moteur_Vision.command" "$RESOURCES/Installer_Moteur_Vision.command"
cp "$ROOT/Installer_Detecteur_IA.command" "$RESOURCES/Installer_Detecteur_IA.command"
cp "$ROOT/autopilot.py" "$RESOURCES/autopilot.py"
cp "$ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
chmod 755 "$MACOS/AMS-Lite-Companion-V2" "$RESOURCES/Installer_Moteur_Vision.command" "$RESOURCES/Installer_Detecteur_IA.command"
chmod 644 "$RESOURCES/ams_companion.py" "$RESOURCES/plate_guardian.py" "$RESOURCES/bambu_camera.py" "$RESOURCES/gcode_mapper.py" "$RESOURCES/local_detector.py" "$RESOURCES/vision_linemod.py" "$RESOURCES/autopilot.py" "$CONTENTS/Info.plist"

# A signed bundle can launch only if each local Python dependency is present
# beside the engine.  Import from the actual Resources directory before
# signing so a forgotten module is caught during the release build.
(
  cd "$RESOURCES"
  PYTHONDONTWRITEBYTECODE=1 python3 -c 'import ams_companion, autopilot, bambu_camera, gcode_mapper, local_detector, plate_guardian, vision_linemod'
)

codesign --force --deep --options runtime --sign "$SIGNING_IDENTITY" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

rm -f "$ARCHIVE"
ditto -c -k --norsrc --keepParent "$APP" "$ARCHIVE"

if [ -n "${NOTARY_PROFILE:-}" ]; then
  if [ "$SIGNING_IDENTITY" = "-" ]; then
    echo "La notarisation exige CODESIGN_IDENTITY=\"Developer ID Application: …\"."
    exit 1
  fi
  xcrun notarytool submit "$ARCHIVE" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
  rm -f "$ARCHIVE"
  ditto -c -k --norsrc --keepParent "$APP" "$ARCHIVE"
fi
rm -rf "$BUILD"

echo
echo "Application créée :"
echo "$APP"
echo
echo "Archive GitHub créée :"
echo "$ARCHIVE"
echo
if [ -t 1 ]; then
  open "$DIST"
fi
