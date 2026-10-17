@echo off
rem Update the Companion Server to the newest release (or git pull for a clone).
rem Stop the server first. Your .env, local config, data and personas are kept;
rem the replaced files are backed up in data\updates\. See docs/updating.md.
setlocal

rem The update may replace this very file, and cmd reads a running batch file as
rem it goes: run a copy from %TEMP% instead.
if /i not "%~1"=="--from-copy" (
  copy /y "%~f0" "%TEMP%\update-companion-run.bat" >nul
  "%TEMP%\update-companion-run.bat" --from-copy "%~dp0"
  exit /b
)
cd /d "%~2"

rem uv through a proxy or antivirus that inspects HTTPS (e.g. Norton).
if not defined UV_NATIVE_TLS set "UV_NATIVE_TLS=1"

where uv >nul 2>nul || (
  echo uv was not found. Install it from https://docs.astral.sh/uv/ and run this again.
  pause
  exit /b 1
)

set "UVARGS_FILE=%TEMP%\companion-update-uvargs.txt"
del "%UVARGS_FILE%" 2>nul
uv run --no-sync python -m companion.updater --extras-file "%UVARGS_FILE%"
set "RC=%ERRORLEVEL%"
if "%RC%"=="3" (
  pause
  exit /b 0
)
if not "%RC%"=="0" (
  echo The update did not finish; nothing more was changed.
  pause
  exit /b 1
)

set "UVARGS=--frozen"
if exist "%UVARGS_FILE%" set /p UVARGS=<"%UVARGS_FILE%"
echo Installing dependencies: uv sync %UVARGS%
uv sync %UVARGS%
set "SYNC=%ERRORLEVEL%"
uv run --no-sync python -m companion.updater --synced %SYNC%
if not "%SYNC%"=="0" (
  echo uv sync failed. Run it again by hand: uv sync %UVARGS%
  pause
  exit /b 1
)

echo.
echo Updated. Start the server again with start-companion.bat.
echo Boards with an older edge client: Devices, Maintenance, Update (in the dashboard).
pause
exit /b 0
