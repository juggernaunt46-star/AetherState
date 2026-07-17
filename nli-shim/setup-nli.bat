@echo off
REM AetherState -- local grounded fact-checker / NLI contradiction setup (Windows).
REM Creates a venv, lets you PICK the checker model, installs the right deps, and starts the
REM shim on 127.0.0.1:8199. The choice is recorded in selected-backend.txt for local reference.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [nli] creating virtual environment...
  python -m venv .venv || goto :err
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -m pip install --upgrade pip >nul

echo.
echo  ============================================================
echo    Pick the grounding / contradiction model:
echo  ============================================================
echo    1) FactCG-DeBERTa-L    (0.4B, MIT)   [recommended - default]
echo         top sub-1B fact-checker (LLM-AggreFact); ~0.8 GB; CPU or GPU.
echo    2) MiniCheck-FT5       (770M, MIT)
echo         Flan-T5 grounded checker, GPT-4-level accuracy; ~1.5 GB.
echo    3) roberta-large-mnli  (0.4B)        [legacy 3-way NLI]
echo         classic entailment/neutral/contradiction; ~1.4 GB.
echo.
set "CH="
set /p "CH=Enter 1, 2 or 3 (default 1): "
set "NLI_BACKEND=factcg"
if "%CH%"=="2" set "NLI_BACKEND=minicheck"
if "%CH%"=="3" set "NLI_BACKEND=nli"

echo.
echo [nli] selected backend: %NLI_BACKEND%
echo [nli] installing dependencies (first run downloads torch + the checker)...
if "%NLI_BACKEND%"=="minicheck" (
  "%PY%" -m pip install "minicheck @ git+https://github.com/Liyan06/MiniCheck.git@main" "accelerate>=0.26.0" sentencepiece truststore || goto :err
)
if "%NLI_BACKEND%"=="nli" (
  "%PY%" -m pip install torch transformers truststore || goto :err
)
if "%NLI_BACKEND%"=="factcg" (
  "%PY%" -m pip install torch transformers sentencepiece truststore || goto :err
)

REM record the choice for local reference
>"selected-backend.txt" echo %NLI_BACKEND%

echo.
echo [nli] starting the shim on http://127.0.0.1:8199  (backend=%NLI_BACKEND%)
echo [nli] the FIRST start downloads the model to your HuggingFace cache.
"%PY%" server.py
goto :eof
:err
echo [nli] setup failed -- see the messages above.
exit /b 1
