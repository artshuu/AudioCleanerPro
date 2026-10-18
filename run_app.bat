@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "app.py" (
    echo [ERROR] app.py not found next to this script.
    pause
    exit /b 1
)

set "PYTHON=venv\Scripts\python.exe"
set "PYTHONW=venv\Scripts\pythonw.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Python environment not found.
    echo.
    echo Run install.bat once to create venv and install dependencies.
    pause
    exit /b 1
)

if /I "%~1"=="--console" goto :console
if /I "%~1"=="--check" (
    echo Launcher OK.
    "%PYTHON%" -c "import sys; print(sys.version)"
    echo Project: %CD%
    exit /b 0
)

rem Normal launch: no console window, the app shows its own error dialogs.
if exist "%PYTHONW%" (
    start "" "%PYTHONW%" "%~dp0app.py"
) else (
    start "" "%PYTHON%" "%~dp0app.py"
)
exit /b 0

:console
rem Diagnostic launch: keeps the console and prints logs there.
"%PYTHON%" "%~dp0app.py"
if errorlevel 1 pause
exit /b %ERRORLEVEL%
