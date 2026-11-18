#!/usr/bin/env bash
# =====================================================================
#  resume-bot SMS forwarder - one-command APK build (Linux / macOS)
#  Requires: Android SDK (platform-34) + JDK 17
# =====================================================================
set -e
cd "$(dirname "$0")"

if [ -z "$ANDROID_HOME" ] && [ -d "$HOME/Android/Sdk" ]; then
  export ANDROID_HOME="$HOME/Android/Sdk"
fi
if [ -z "$ANDROID_HOME" ]; then
  echo "[ERR] Android SDK not found. Set ANDROID_HOME or install Android Studio (SDK platform 34)."
  exit 1
fi
echo "[OK] ANDROID_HOME=$ANDROID_HOME"

if [ ! -f gradlew ]; then
  if command -v gradle >/dev/null 2>&1; then
    echo "[INFO] Generating gradle wrapper..."
    gradle wrapper --gradle-version 8.9
  else
    echo "[INFO] No gradlew and no system 'gradle'. Open this folder in Android Studio -> Build -> Generate Signed Bundle/APK."
    exit 0
  fi
fi

echo "[INFO] Building release APK..."
./gradlew assembleRelease
echo
echo "[OK] APK: app/build/outputs/apk/release/app-release-unsigned.apk"
echo "      Install: adb install app/build/outputs/apk/release/app-release-unsigned.apk"
