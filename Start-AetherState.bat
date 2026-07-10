@echo off
setlocal
cd /d "%~dp0"
echo.
echo  ==============================
echo    AetherState
echo  ==============================
echo.
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python 3.10 or newer is required. Download it from:
  echo   https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo First run: installing into a private environment - takes a minute or two...
  %PY% -m venv .venv
  if errorlevel 1 ( echo Could not create the Python environment. & pause & exit /b 1 )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -e .
  if errorlevel 1 (
    echo.
    echo Install failed. If the errors above mention SSL, your antivirus intercepts
    echo downloads - run this once, then start this script again:
    echo   .venv\Scripts\python.exe -m pip install --use-feature=truststore -e .
    pause
    exit /b 1
  )
)
if not exist "aetherstate-data" mkdir "aetherstate-data"
if not exist "aetherstate-data\config.toml" copy /y "config.example.toml" "aetherstate-data\config.toml" >nul
echo Starting AetherState on http://127.0.0.1:9130 ...
echo The Console will open in your browser. Keep this window open while you play.
echo.
start "" cmd /c "timeout /t 4 >nul & start "" http://127.0.0.1:9130/aether/console"
".venv\Scripts\python.exe" -m aetherstate
echo.
echo AetherState stopped.
pause
