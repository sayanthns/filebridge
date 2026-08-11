#!/bin/bash
# Assembles FileBridge.app and installs it to ~/Applications.
#
# The bundle is self-contained on purpose. Keeping the sources outside it and
# referencing them failed: if the code sits in ~/Documents, a Finder-launched
# app cannot read it (macOS TCC) and dies with "Operation not permitted".
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/FileBridge.app"
VERSION="$(grep -o 'APP_VERSION = "[^"]*"' "$HERE/filebridge.py" | cut -d'"' -f2)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/tools"
cp "$HERE/filebridge.py" "$APP/Contents/Resources/"
cp "$HERE/tools/"*.js "$HERE/tools/make_qr.sh" "$APP/Contents/Resources/tools/"
chmod +x "$APP/Contents/Resources/tools/make_qr.sh"
cp "$HERE/launcher.sh" "$APP/Contents/MacOS/FileBridge"
chmod +x "$APP/Contents/MacOS/FileBridge"

# Finder Quick Actions ride inside the bundle; the launcher installs them into
# ~/Library/Services on first run and refreshes them when this version changes.
/usr/bin/python3 "$HERE/scripts/make_quick_actions.py" "$APP/Contents/Resources/QuickActions" >/dev/null
echo "$VERSION" > "$APP/Contents/Resources/QuickActions/VERSION"

if [ -f "$HERE/docs/icon/FileBridge.icns" ]; then
  cp "$HERE/docs/icon/FileBridge.icns" "$APP/Contents/Resources/FileBridge.icns"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>FileBridge</string>
    <key>CFBundleDisplayName</key><string>File Bridge</string>
    <key>CFBundleExecutable</key><string>FileBridge</string>
    <key>CFBundleIdentifier</key><string>com.enfono.filebridge.mac</string>
    <key>CFBundleIconFile</key><string>FileBridge</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

plutil -lint "$APP/Contents/Info.plist" >/dev/null
echo "built $APP (version $VERSION)"
