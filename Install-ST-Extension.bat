@echo off
setlocal
cd /d "%~dp0"
echo This installs the AetherState Companion extension into SillyTavern.
echo.
set /p STDIR="Paste your SillyTavern folder path (the folder that contains Start.bat): "
if "%STDIR%"=="" ( echo No path given. & pause & exit /b 1 )
if not exist "%STDIR%\data\default-user" (
  echo That does not look like a SillyTavern folder ^(no data\default-user inside^).
  pause
  exit /b 1
)
set "DEST=%STDIR%\data\default-user\extensions\AetherState"
xcopy /e /i /y "st-extension" "%DEST%" >nul
if errorlevel 1 ( echo Copy failed. & pause & exit /b 1 )
echo.
echo Installed to: %DEST%
echo Now restart SillyTavern and hard-refresh your browser (Ctrl+Shift+R).
pause
