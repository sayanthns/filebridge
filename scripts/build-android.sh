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
  # Must be Gradle 8. AGP 8.2.1 dies on Gradle 9 with "Could not isolate value
  # ... BuildFlowService$Parameters", because it reaches for
  # org/gradle/api/internal/HasConvention, which 9 removed. Newest-wins picked
  # a cached 9.x the moment one appeared, so ask for 8.2.1 by name first - it
  # is the version every APK here was built with - and fall back to the newest
  # other 8.x only if it is gone.
  DISTS="$HOME/.gradle/wrapper/dists"
  GRADLE="$(find "$DISTS" -maxdepth 5 -type f -name gradle -perm +111 2>/dev/null \
            | grep -E '/gradle-8\.2\.1/' | head -1)"
  [ -n "$GRADLE" ] || GRADLE="$(find "$DISTS" -maxdepth 5 -type f -name gradle -perm +111 \
            2>/dev/null | grep -E '/gradle-8\.' | sort -V | tail -1)"
fi
[ -n "$GRADLE" ] || { echo "No gradle found. brew install gradle"; exit 1; }

"$GRADLE" --no-daemon assembleDebug
APK="app/build/outputs/apk/debug/app-debug.apk"
VER="$("$ANDROID_HOME/build-tools/34.0.0/aapt" dump badging "$APK" | sed -n "s/.*versionName='\([^']*\)'.*/\1/p")"
cp "$APK" "$HERE/FileBridge-$VER.apk"
# Also into dist/, which is the copy that gets committed and attached to the
# release. The root copy is a convenience for `cp`-ing to to-phone and stays
# gitignored; dist/ is the one that survives.
mkdir -p "$HERE/dist"
cp "$APK" "$HERE/dist/FileBridge-$VER.apk"
echo "built FileBridge-$VER.apk (and dist/FileBridge-$VER.apk)"
