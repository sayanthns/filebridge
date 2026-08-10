#!/bin/bash
# Builds the debug APK. Needs Android SDK + JDK 17; no Android Studio required.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
cd "$HERE/android"
echo "sdk.dir=$ANDROID_HOME" > local.properties

GRADLE="${GRADLE:-$(command -v gradle || true)}"
if [ -z "$GRADLE" ]; then
  GRADLE="$(find "$HOME/.gradle/wrapper/dists" -maxdepth 5 -type f -name gradle -perm +111 2>/dev/null | sort | tail -1)"
fi
[ -n "$GRADLE" ] || { echo "No gradle found. brew install gradle"; exit 1; }

"$GRADLE" --no-daemon assembleDebug
APK="app/build/outputs/apk/debug/app-debug.apk"
VER="$("$ANDROID_HOME/build-tools/34.0.0/aapt" dump badging "$APK" | sed -n "s/.*versionName='\([^']*\)'.*/\1/p")"
cp "$APK" "$HERE/FileBridge-$VER.apk"
echo "built FileBridge-$VER.apk"
