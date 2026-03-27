@echo off
title VoxCraft
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ================================================
echo   VoxCraft - Audio Transcription Tool
echo ================================================
echo.

:: Check files
if not exist "%~dp0server.py" (
    echo ERROR: server.py not found in %~dp0
    echo Make sure start.bat and server.py are in the same folder.
    pause & exit /b 1
)
if not exist "%~dp0index.html" (
    echo ERROR: index.html not found in %~dp0
    pause & exit /b 1
)
echo [OK] Files found

:: Check Python
set PYTHON=
python --version >nul 2>&1
if %errorlevel% equ 0 ( set PYTHON=python & goto :py_ok )
python3 --version >nul 2>&1
if %errorlevel% equ 0 ( set PYTHON=python3 & goto :py_ok )
echo ERROR: Python not found.
echo Download Python 3.8+ from https://www.python.org/downloads/
echo During install check "Add Python to PATH"
pause & exit /b 1
:py_ok
echo [OK] Python found

:: Check ffmpeg
if exist "%~dp0ffmpeg.exe" ( goto :ffmpeg_ok )
where ffmpeg >nul 2>&1
if %errorlevel% equ 0 ( goto :ffmpeg_ok )

echo [..] ffmpeg not found. Trying winget...
winget --version >nul 2>&1
if %errorlevel% equ 0 (
    winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements
    where ffmpeg >nul 2>&1
    if %errorlevel% equ 0 ( echo [OK] ffmpeg installed via winget & goto :ffmpeg_ok )
)

echo [..] Downloading ffmpeg (~45MB)...
set FURL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
set FZIP=%TEMP%\ffmpeg_vc.zip
set FDIR=%TEMP%\ffmpeg_vc_ext

if exist "%FDIR%" rmdir /s /q "%FDIR%" >nul 2>&1
if exist "%FZIP%" del "%FZIP%" >nul 2>&1

powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%FURL%' -OutFile '%FZIP%' -UseBasicParsing -TimeoutSec 180; exit 0 } catch { exit 1 }"
if %errorlevel% neq 0 goto :ffmpeg_fail
if not exist "%FZIP%" goto :ffmpeg_fail

echo [..] Extracting ffmpeg...
powershell -NoProfile -Command "Add-Type -A System.IO.Compression.FileSystem; [IO.Compression.ZipFile]::ExtractToDirectory('%FZIP%','%FDIR%')"

for /r "%FDIR%" %%f in (ffmpeg.exe) do (
    copy "%%f" "%~dp0ffmpeg.exe" >nul 2>&1
    if exist "%FZIP%" del "%FZIP%" >nul 2>&1
    if exist "%FDIR%" rmdir /s /q "%FDIR%" >nul 2>&1
    echo [OK] ffmpeg installed to project folder
    goto :ffmpeg_ok
)

:ffmpeg_fail
echo.
echo [WARN] ffmpeg auto-install failed.
echo Run get_ffmpeg.bat for manual install options.
echo Video files need ffmpeg. Audio (MP3/WAV) works without it.
echo.
goto :ffmpeg_skip

:ffmpeg_ok
echo [OK] ffmpeg ready
:ffmpeg_skip

:: Start server
echo.
echo [3/3] Starting VoxCraft server...
echo.
echo  ================================
echo   URL  :  http://127.0.0.1:8800
echo   Stop :  Ctrl+C
echo  ================================
echo.
echo  TIP: Large files (500MB+) use "Path on disk" mode
echo       Enter full path: D:\Videos\file.mp4
echo.

start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8800"
%PYTHON% "%~dp0server.py"

echo.
echo Server stopped.
pause
