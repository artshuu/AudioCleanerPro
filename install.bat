@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "CHECK_ONLY=0"
set "MAKE_SHORTCUT=1"
if /I "%~1"=="--check" set "CHECK_ONLY=1"
if /I "%~1"=="--no-shortcut" set "MAKE_SHORTCUT=0"
if /I "%~2"=="--no-shortcut" set "MAKE_SHORTCUT=0"

echo ============================================
echo   Audio Cleaner Pro - environment setup
echo ============================================
echo.

rem --- 1. Find Python 3.12+ ---
set "PY_CMD="
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3.12"
if not defined PY_CMD (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)
if not defined PY_CMD (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [ERROR] Python not found.
    echo Install Python 3.12 from https://www.python.org/downloads/windows/
    echo Enable "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

%PY_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.12 or newer is required.
    echo Found: %PY_CMD%
    echo Install Python 3.12 and run install.bat again.
    echo.
    pause
    exit /b 1
)
echo Python: %PY_CMD%

if "%CHECK_ONLY%"=="1" goto :check

rem --- 2. Virtual environment ---
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment venv...
    %PY_CMD% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists.
)

set "PYTHON=venv\Scripts\python.exe"

rem --- 3. Dependencies ---
echo Updating pip...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 echo [WARNING] pip upgrade failed, continuing.

echo Installing dependencies (may take several minutes)...
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install dependencies.
    echo Check your internet connection and run install.bat again.
    echo If venv is broken, delete the venv folder and rerun install.bat.
    echo.
    pause
    exit /b 1
)

:check
set "PYTHON=venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] venv not found.
    echo Run install.bat without --check.
    echo.
    if "%CHECK_ONLY%"=="0" pause
    exit /b 1
)

echo Checking component imports...
"%PYTHON%" -c "import customtkinter, soundfile, noisereduce, vosk, silero_vad, scipy, numpy, torch, torchaudio"
if errorlevel 1 (
    echo [ERROR] Some components are missing.
    echo Delete the venv folder and run install.bat again.
    echo.
    if "%CHECK_ONLY%"=="0" pause
    exit /b 1
)

rem --- 4. Model and dictionary ---
if not exist "words.txt" (
    echo [WARNING] words.txt not found: censorship will run in limited mode.
)

set "MODEL_FOUND="
if exist "vosk-model-ru-0.42\am" set "MODEL_FOUND=vosk-model-ru-0.42"
if not defined MODEL_FOUND if exist "vosk-model-ru-0.22\am" set "MODEL_FOUND=vosk-model-ru-0.22"
if not defined MODEL_FOUND if exist "vosk-model-small-ru-0.22\am" set "MODEL_FOUND=vosk-model-small-ru-0.22"
if defined MODEL_FOUND (
    echo Vosk model: %MODEL_FOUND%
) else (
    echo [WARNING] Vosk model not found.
    echo Download vosk-model-small-ru-0.22:
    echo   https://alphacephei.com/vosk/models
    echo and unpack it into the project folder.
)

if "%CHECK_ONLY%"=="1" (
    echo.
    echo Check completed.
    endlocal
    exit /b 0
)

rem --- 5. Desktop shortcut ---
if not "%MAKE_SHORTCUT%"=="1" goto :after_shortcut
echo Creating desktop shortcut...
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"
"%PS%" -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'AudioCleanerPro.lnk')); $s.TargetPath = (Join-Path '%CD%' 'venv\Scripts\pythonw.exe'); $s.Arguments = [char]34 + (Join-Path '%CD%' 'app.py') + [char]34; $s.WorkingDirectory = '%CD%'; $s.IconLocation = (Join-Path '%CD%' 'venv\Scripts\python.exe') + ',0'; $s.Save()"
if errorlevel 1 echo [WARNING] Could not create shortcut. Use run_app.bat.

:after_shortcut
echo.
echo Done.
echo Launch options:
echo   - desktop shortcut AudioCleanerPro (no console)
echo   - run_app.bat (normal launch)
echo   - run_app.bat --console (with console and messages)
echo   - AudioCleanerPro.vbs (silent launch)
echo.
pause
endlocal
