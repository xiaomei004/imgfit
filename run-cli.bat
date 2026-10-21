@echo off
rem imgfit CLI launcher - no installation required.
rem Usage: drag a folder onto this file, or run in cmd:
rem   run-cli.bat "D:\materials"
rem   run-cli.bat "D:\materials" -t 500kb -o "D:\out"
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
rem force UTF-8 output so Chinese text stays readable in pipes
set "PYTHONUTF8=1"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m imgfit %*
if errorlevel 1 pause
