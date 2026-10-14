@echo off
rem Start the Companion Server and open its admin dashboard.
rem If the server is already running, just opens the dashboard.
setlocal
cd /d "%~dp0"

rem uv through a proxy or antivirus that inspects HTTPS (e.g. Norton).
if not defined UV_NATIVE_TLS set "UV_NATIVE_TLS=1"

where uv >nul 2>nul || (
  echo uv was not found. Install it from https://docs.astral.sh/uv/ and run this again.
  pause
  exit /b 1
)

rem The address the server listens on, from configs\companion.yaml (+ .local.yaml).
set "URL="
for /f "usebackq delims=" %%U in (`uv run python -c "from companion.core.config import load_config; s = load_config().server; h = '127.0.0.1' if s.host in ('0.0.0.0', '::', '') else s.host; h = f'[{h}]' if ':' in h else h; print(f'http://{h}:{s.port}')"`) do set "URL=%%U"
if not defined URL (
  echo Could not read the configuration. Run "uv run companion check" to see why.
  pause
  exit /b 1
)

curl -s -f -o nul "%URL%/health" && (
  echo Companion Server is already running at %URL%
  start "" "%URL%/admin/"
  exit /b 0
)

echo Starting Companion Server at %URL% ...
start "Companion Server" cmd /k uv run companion

rem Wait for it (the first start can take a while to load speech recognition).
for /l %%i in (1,1,120) do (
  curl -s -f -o nul "%URL%/health" && goto ready
  timeout /t 1 /nobreak >nul
)
echo The server did not answer within 2 minutes; see the "Companion Server" window.
pause
exit /b 1

:ready
echo Opening the dashboard: %URL%/admin/
start "" "%URL%/admin/"
exit /b 0
