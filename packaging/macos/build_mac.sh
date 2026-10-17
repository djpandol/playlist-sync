#!/bin/bash
# Build "Playlist Sync.app" and PlaylistSync-<version>-mac-arm64.dmg on an Apple Silicon Mac (or GitHub macos-14).
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 -m pip install --upgrade pip
python3 -m pip install "pyrekordbox==0.4.4" "pywebview>=5,<7" "pyinstaller>=6.10" pip-licenses
python3 build.py
python3 -m py_compile playlist_sync.py
python3 playlist_sync.py --selftest || true          # environment report (no library on CI)
python3 packaging/make_notices.py THIRD_PARTY_NOTICES.txt
rm -rf build dist
python3 -m PyInstaller --noconfirm --clean packaging/macos/PlaylistSync.spec
VER=$(python3 -c "import re;print(re.search(r'^VERSION = \"([^\"]+)\"',open('src/engine.py').read(),re.M).group(1))")
APP="dist/Playlist Sync.app"
codesign --verify --deep "$APP" && echo "codesign: ad-hoc signature OK"
# smoke test: the frozen app must import pyrekordbox + sqlcipher and open an encrypted DB
SELF=$("$APP/Contents/MacOS/Playlist Sync" --selftest 2>&1 || true); echo "$SELF"
echo "$SELF" | grep -q "pyrekordbox: OK" || { echo "FROZEN APP CANNOT IMPORT pyrekordbox"; exit 1; }
"$APP/Contents/MacOS/Playlist Sync" --sqlcipher-check || { echo "FROZEN APP SQLCIPHER CHECK FAILED"; exit 1; }
STAGE=$(mktemp -d); cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
cp packaging/macos/README_FIRST.txt "$STAGE/처음 실행 방법 (Read me first).txt"
DMG="dist/PlaylistSync-$VER-mac-arm64.dmg"
hdiutil create -volname "Playlist Sync $VER" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
du -sh "$APP" "$DMG"; shasum -a 256 "$DMG" | tee "$DMG.sha256"
