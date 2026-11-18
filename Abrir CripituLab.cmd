@echo off
if not exist "%~dp0.local\venv\Scripts\python.exe" (
    echo Execute primeiro Instalar no Resolve.cmd.
    pause
    exit /b 1
)
"%~dp0.local\venv\Scripts\python.exe" "%~dp0CripituLab.py" %*
if errorlevel 1 pause
