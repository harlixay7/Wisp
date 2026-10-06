@echo off
setlocal
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
if not defined ANTIGRAVITY_WORKSPACE set "ANTIGRAVITY_WORKSPACE=%REPO_ROOT%"
if not defined ANTIGRAVITY_MODEL set "ANTIGRAVITY_MODEL=gemini-3.8-flash-high"
if not defined ANTIGRAVITY_FALLBACK_MODEL set "ANTIGRAVITY_FALLBACK_MODEL=claude-opus-4-6-thinking"
if exist "%REPO_ROOT%\.venv\Scripts\pythonw.exe" (
  start "" "%REPO_ROOT%\.venv\Scripts\pythonw.exe" "%REPO_ROOT%\tools\antigravity_viewer.py" %*
  exit /b 0
)
where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
  start "" pythonw "%REPO_ROOT%\tools\antigravity_viewer.py" %*
  exit /b 0
)
python "%REPO_ROOT%\tools\antigravity_viewer.py" %*
exit /b %ERRORLEVEL%
