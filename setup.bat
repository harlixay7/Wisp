@echo off
setlocal EnableExtensions
title Wisp setup
cd /d "%~dp0"

rem Finds Python 3.10+ (offering to install it with winget when there is none)
rem and hands over to tools\wisp_setup.py. Arguments are forwarded unchanged:
rem   setup.bat              install and verify Wisp
rem   setup.bat --connect    connect Claude Code, Codex or another assistant
rem   setup.bat --check      report on the setup without changing anything
rem   setup.bat --yes        accept every offer without asking
rem   setup.bat --dev        also install the test tools and run the suite
rem   setup.bat --no-widget  skip the Electron desktop overlay
rem   setup.bat --no-pause   do not wait for a key press at the end

set "NOPAUSE="
set "AUTOYES="
for %%A in (%*) do (
  if /i "%%~A"=="--no-pause" set "NOPAUSE=1"
  if /i "%%~A"=="--yes" set "AUTOYES=1"
  if /i "%%~A"=="-y" set "AUTOYES=1"
)

call :find_python
if defined PY goto :run

echo.
echo  Wisp needs Python 3.10 or newer, and none was found.
where winget >nul 2>nul
if errorlevel 1 goto :no_python
if defined AUTOYES goto :install_python
choice /c YN /n /m " Install Python 3.12 now with winget? [Y/N] "
if errorlevel 2 goto :no_python

:install_python
echo  Installing Python 3.12. This can take a minute ...
winget install --exact --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto :run
rem winget does not refresh PATH for this window, so look where it installs.
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :run

:no_python
echo.
echo  Install Python 3.10 or newer from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH" in the installer), then run setup.bat again.
set "CODE=1"
goto :done

:run
%PY% "%~dp0tools\wisp_setup.py" %*
set "CODE=%ERRORLEVEL%"

:done
echo.
if not defined NOPAUSE pause
exit /b %CODE%

:find_python
set "PY="
where py >nul 2>nul && py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if defined PY exit /b 0
where python >nul 2>nul && python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
exit /b 0
