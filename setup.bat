@echo off
setlocal EnableExtensions
title Wisp setup
cd /d "%~dp0"

rem Finds a Python 3.10+ interpreter and hands over to tools\wisp_setup.py,
rem which does the real work. Arguments are forwarded unchanged:
rem   setup.bat              set up .venv, dependencies and the desktop widget
rem   setup.bat --check      report on the setup without changing anything
rem   setup.bat --dev        also install the test tools and run the suite
rem   setup.bat --no-widget  skip the Electron desktop overlay
rem   setup.bat --recreate-venv  delete and rebuild .venv first
rem   setup.bat --no-pause   do not wait for a key press at the end

set "NOPAUSE="
for %%A in (%*) do if /i "%%~A"=="--no-pause" set "NOPAUSE=1"

set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  echo.
  echo  Wisp needs Python 3.10 or newer, and none was found.
  echo  Install it from https://www.python.org/downloads/ ^(tick "Add python.exe
  echo  to PATH" in the installer^), then run setup.bat again.
  set "CODE=1"
  goto :done
)

%PY% "%~dp0tools\wisp_setup.py" %*
set "CODE=%ERRORLEVEL%"

:done
echo.
if not defined NOPAUSE pause
exit /b %CODE%
