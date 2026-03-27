@echo off
title Install ffmpeg for VoxCraft
cd /d "%~dp0"
echo ================================================
echo   ffmpeg installer for VoxCraft
echo ================================================
echo.

:: Already have it?
if exist "%~dp0ffmpeg.exe" (
    echo [OK] ffmpeg.exe already exists in this folder!
    echo Path: %~dp0ffmpeg.exe
    goto :done
)
where ffmpeg >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] ffmpeg found in system PATH
    for /f "tokens=*" %%i in ('where ffmpeg') do echo Path: %%i
    goto :done
)

echo [..] ffmpeg not found. Choose install method:
echo.
echo  1. winget (Windows 10/11 built-in, recommended)
echo  2. Download zip from gyan.dev (~45MB)
echo  3. Manual - I will open download page
echo.
set /p CHOICE=Enter choice (1/2/3): 

if "%CHOICE%"=="1" goto :install_winget
if "%CHOICE%"=="2" goto :install_zip
if "%CHOICE%"=="3" goto :install_manual
goto :install_zip

:install_winget
echo.
echo [..] Running: winget install --id Gyan.FFmpeg -e
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
where ffmpeg >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] ffmpeg installed! Restart start.bat now.
) else (
    echo [WARN] winget install may need a new CMD window to take effect.
    echo Try closing and reopening start.bat
)
goto :done

:install_zip
echo.
echo [..] Downloading ffmpeg from gyan.dev...
echo     Size: ~45MB, please wait...
set FURL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
set FZIP=%~dp0ffmpeg_download.zip
set FDIR=%~dp0ffmpeg_extract

if exist "%FDIR%" rmdir /s /q "%FDIR%" >nul 2>&1
if exist "%FZIP%" del "%FZIP%" >nul 2>&1

powershell -NoProfile -Command ^
  "Write-Host 'Downloading...';" ^
  "$ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest -Uri '%FURL%' -OutFile '%FZIP%' -UseBasicParsing -TimeoutSec 180;" ^
  "Write-Host 'Done!'"

if not exist "%FZIP%" (
    echo ERROR: Download failed!
    echo Try manual install from https://www.gyan.dev/ffmpeg/builds/
    goto :done
)

echo [..] Extracting...
powershell -NoProfile -Command ^
  "Add-Type -A System.IO.Compression.FileSystem;" ^
  "[IO.Compression.ZipFile]::ExtractToDirectory('%FZIP%','%FDIR%')"

set FOUND=0
for /r "%FDIR%" %%f in (ffmpeg.exe) do (
    copy "%%f" "%~dp0ffmpeg.exe" >nul 2>&1
    set FOUND=1
    echo [OK] ffmpeg.exe copied to: %~dp0ffmpeg.exe
    goto :extracted
)
:extracted
if exist "%FZIP%" del "%FZIP%" >nul 2>&1
if exist "%FDIR%" rmdir /s /q "%FDIR%" >nul 2>&1

if %FOUND%==1 (
    echo.
    echo  SUCCESS! Now run start.bat to launch VoxCraft.
) else (
    echo ERROR: Could not find ffmpeg.exe in archive
)
goto :done

:install_manual
start https://www.gyan.dev/ffmpeg/builds/
echo.
echo  Download "ffmpeg-release-essentials.zip"
echo  Extract and copy ffmpeg.exe to this folder:
echo  %~dp0
goto :done

:done
echo.
pause

