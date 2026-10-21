@echo off
rem imgfit GUI launcher - no installation required.
rem Double-click this file to open the window.
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m imgfit --gui
if errorlevel 1 (
  echo.
  echo Failed to start. Make sure Python 3.9+ is installed and on PATH.
  pause
)
