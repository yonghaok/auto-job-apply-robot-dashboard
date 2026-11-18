@echo off
REM =====================================================================
REM  resume-bot SMS forwarder - one-command APK build (Windows)
REM  Requires: Android SDK (platform-34) + JDK 17
REM =====================================================================
setlocal
cd /d %~dp0

REM --- locate Android SDK ---
if defined ANDROID_HOME goto :have_sdk
if exist "%LOCALAPPDATA%\Android\Sdk" (
    set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
    goto :have_sdk
)
echo [ERR] Android SDK not found. Install Android Studio (SDK platform 34) or
echo       set ANDROID_HOME, then rerun this script.
exit /b 1
:have_sdk
echo [OK] ANDROID_HOME=%ANDROID_HOME%

REM --- ensure gradle wrapper exists ---
if not exist "gradlew.bat" (
    where gradle >nul 2>nul
    if errorlevel 1 (
        echo [INFO] No gradlew.bat and no system "gradle". Easiest path:
        echo        Open this folder in Android Studio -> Build -> Generate Signed Bundle/APK.
        echo        (Android Studio downloads the wrapper + SDK automatically.)
        exit /b 0
    )
    echo [INFO] Generating gradle wrapper (needs system gradle)...
    call gradle wrapper --gradle-version 8.9
)

echo [INFO] Building release APK...
call gradlew.bat assembleRelease
if errorlevel 1 (
    echo [ERR] Build failed. See output above.
    exit /b 1
)
echo.
echo [OK] APK: app\build\outputs\apk\release\app-release-unsigned.apk
echo      Install:  adb install app\build\outputs\apk\release\app-release-unsigned.apk
echo      (For a signed release, use Android Studio's "Generate Signed Bundle/APK".)
endlocal
