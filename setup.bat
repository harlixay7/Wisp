@echo off
setlocal EnableExtensions
title Wisp setup
cd /d "%~dp0"
echo.
echo  Wisp - setup and environment check
echo  ==================================
echo.

rem ---- 1. Python 3.10+ ------------------------------------------------------
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [X] Python was not found on PATH.
  echo     Install Python 3.10 or newer from https://www.python.org/downloads/
  echo     and enable "Add python.exe to PATH", then re-run this script.
  goto :fail
)
%PY% -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [X] Python 3.10 or newer is required.
  goto :fail
)
for /f "delims=" %%V in ('%PY% -c "import platform; print(platform.python_version())"') do echo [ok] Python %%V

rem ---- 2. Virtual environment and dependencies -----------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [..] Creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)
set "VENV=%CD%\.venv\Scripts\python.exe"
"%VENV%" -m pip install --quiet --upgrade pip
"%VENV%" -m pip install --quiet -r requirements-dev.txt
if errorlevel 1 goto :fail
echo [ok] Dependencies installed

rem ---- 3. Antigravity CLI (agy) --------------------------------------------
set "AGY="
for /f "delims=" %%A in ('where agy 2^>nul') do if not defined AGY set "AGY=%%A"
if not defined AGY if exist "%USERPROFILE%\.gemini\bin\agy.exe" set "AGY=%USERPROFILE%\.gemini\bin\agy.exe"
if not defined AGY (
  echo [!] Antigravity CLI ^(agy^) was not found on PATH or under %%USERPROFILE%%\.gemini\bin.
  echo     Install the Google Antigravity CLI, sign in once with "agy", then re-run
  echo     this script. The bridge also accepts an explicit path:
  echo       tools\antigravity_bridge.py --executable ^<path-to-agy^>
) else (
  echo [ok] Antigravity CLI: %AGY%
  echo [..] Running "agy install" to configure environment paths ...
  call "%AGY%" install >nul 2>nul
  if errorlevel 1 echo [!] "agy install" reported a problem; run it manually if commands misbehave.
)

rem ---- 4. Skill registry validation and deterministic test suite -----------
"%VENV%" -m tools.skill_loader --validate
if errorlevel 1 goto :fail
"%VENV%" -m pytest tests/ -q
if errorlevel 1 goto :fail

rem ---- 5. Electron widget shell (optional) ---------------------------------
if exist "tools\wisp_shell\package.json" (
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [!] Node.js/npm not found - the desktop widget shell is skipped.
    echo     The viewer still runs in a browser; install Node.js to get the widget.
  ) else (
    if not exist "tools\wisp_shell\node_modules\electron\dist\electron.exe" (
      echo [..] Installing the Electron shell ^(one-time^) ...
      pushd "tools\wisp_shell"
      call npm install --no-audit --no-fund
      if errorlevel 1 echo [!] npm install failed; the browser fallback will be used.
      popd
    ) else (
      echo [ok] Electron shell present
    )
  )
)

echo.
echo  Setup complete. Next steps:
echo    - Bridge status:   .venv\Scripts\python tools\antigravity_bridge.py --status
echo    - Live viewer:     tools\antigravity_viewer.cmd
echo    - MCP registration: see README "Register the MCP server".
echo.
pause
exit /b 0

:fail
echo.
echo  Setup failed. Review the messages above.
pause
exit /b 1
