@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ==========================================
echo    AetherState + SillyTavern quick install
echo  ==========================================
echo.

set "STDIR="
set "INSTALL_ONLY="

if /i "%~1"=="--install-only" (
  set "INSTALL_ONLY=1"
) else if not "%~1"=="" (
  set "STDIR=%~1"
)
if /i "%~2"=="--install-only" set "INSTALL_ONLY=1"

if not defined STDIR if defined SILLYTAVERN_DIR call :probe "%SILLYTAVERN_DIR%"
if not defined STDIR call :probe "%CD%\SillyTavern"
if not defined STDIR call :probe "%CD%\..\SillyTavern"
if not defined STDIR call :probe "%USERPROFILE%\SillyTavern"
if not defined STDIR call :probe "%USERPROFILE%\Documents\SillyTavern"
if not defined STDIR if defined LOCALAPPDATA call :probe "%LOCALAPPDATA%\SillyTavern"

if not defined STDIR (
  echo SillyTavern was not found automatically.
  set /p "STDIR=Paste its folder path, or press Enter to install only AetherState: "
)

if defined STDIR (
  call :install_companion "%STDIR%"
  if errorlevel 1 exit /b 1
) else (
  echo Companion install skipped. You can run Install-ST-Extension.bat later.
)

if defined INSTALL_ONLY (
  echo Install-only verification complete.
  exit /b 0
)

echo Starting AetherState setup and Console...
call "%CD%\Start-AetherState.bat"
exit /b %ERRORLEVEL%

:probe
if defined STDIR exit /b 0
if exist "%~1\data\default-user\" set "STDIR=%~f1"
exit /b 0

:install_companion
set "STDIR=%~f1"
if not exist "%STDIR%\data\default-user\" (
  echo.
  echo That folder is not a ready SillyTavern install: "%STDIR%"
  echo Start SillyTavern once so data\default-user exists, then run this installer again.
  exit /b 1
)
set "DEST=%STDIR%\data\default-user\extensions\AetherState"
if not exist "%DEST%" mkdir "%DEST%"
xcopy /e /i /y "st-extension" "%DEST%" >nul
if errorlevel 1 (
  echo Companion copy failed.
  exit /b 1
)
for %%F in (manifest.json index.js style.css) do (
  fc /b "st-extension\%%F" "%DEST%\%%F" >nul
  if errorlevel 1 (
    echo Companion verification failed for %%F.
    exit /b 1
  )
)
echo AetherState Companion installed in SillyTavern.
exit /b 0
