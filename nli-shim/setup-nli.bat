@echo off
REM AetherState -- local NLI contradiction model setup (Windows).
REM Creates a venv, installs CPU torch + transformers, and starts the shim on 127.0.0.1:8199.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [nli] creating virtual environment...
  python -m venv .venv || goto :err
)
echo [nli] installing dependencies (first run downloads torch + transformers)...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install torch transformers || goto :err
echo.
echo [nli] starting the shim on http://127.0.0.1:8199
echo [nli] (the FIRST start downloads the model, ~1.4 GB, to your HuggingFace cache)
".venv\Scripts\python.exe" server.py
goto :eof
:err
echo [nli] setup failed -- see the messages above.
exit /b 1
