@echo off
setlocal
rem Starts the Wisp MCP server with the .venv Python. The server reviews the
rem folder the assistant starts it in; set ANTIGRAVITY_WORKSPACE to pin one.
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
if not defined ANTIGRAVITY_MODEL set "ANTIGRAVITY_MODEL=gemini-3.8-flash-high"
if not defined ANTIGRAVITY_FALLBACK_MODEL set "ANTIGRAVITY_FALLBACK_MODEL=claude-opus-4-6-thinking"
set "PY=python"
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
"%PY%" "%REPO_ROOT%\tools\antigravity_mcp_server.py" %*
exit /b %ERRORLEVEL%
