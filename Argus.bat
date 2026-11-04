@echo off
setlocal
cd /d "%~dp0"
if not exist "Src\argus_app.py" (
  echo ERROR: Src\argus_app.py not found.
  pause
  exit /b 1
)
if not exist "vault" mkdir "vault"
if not exist ".env" (
  echo WARNING: .env not found. Copy .env.example to .env and configure your keys.
)
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  where py >nul 2>&1
  if not errorlevel 1 (
    set "PY=py -3"
  ) else (
    set "PY=python"
  )
)
cd Src
%PY% -m streamlit run argus_app.py --server.address 127.0.0.1 --server.port 8501 --theme.base dark --theme.primaryColor "#D9B44A" --theme.backgroundColor "#0F1E24" --theme.secondaryBackgroundColor "#16303A" --theme.textColor "#E9F1EF"
if errorlevel 1 pause
