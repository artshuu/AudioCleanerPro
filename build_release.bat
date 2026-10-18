@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist "venv\Scripts\python.exe" set "PYTHON=venv\Scripts\python.exe"

echo === AudioCleanerPro Release Build ===
echo Using: %PYTHON%

%PYTHON% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found, installing...
    %PYTHON% -m pip install --upgrade pip
    %PYTHON% -m pip install pyinstaller
)

if exist build rd /s /q build
if exist dist rd /s /q dist

%PYTHON% -m PyInstaller --clean --noconfirm AudioCleanerPro.spec
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

set "APP_DIR=dist\AudioCleanerPro"
if exist "%APP_DIR%\_internal" (
    set "RES_DIR=%APP_DIR%\_internal"
) else (
    set "RES_DIR=%APP_DIR%"
)

echo Checking bundled resources in %RES_DIR% ...
if not exist "%RES_DIR%\words.txt" copy /y "words.txt" "%RES_DIR%\" >nul

set "MODEL_NAME="
if exist "vosk-model-ru-0.42\am" set "MODEL_NAME=vosk-model-ru-0.42"
if not defined MODEL_NAME if exist "vosk-model-ru-0.22\am" set "MODEL_NAME=vosk-model-ru-0.22"
if not defined MODEL_NAME if exist "vosk-model-small-ru-0.22\am" set "MODEL_NAME=vosk-model-small-ru-0.22"
if defined MODEL_NAME if not exist "%RES_DIR%\%MODEL_NAME%\am" (
    xcopy "%MODEL_NAME%" "%RES_DIR%\%MODEL_NAME%" /e /i /y >nul
)

if not exist "%APP_DIR%\AudioCleanerPro.exe" (
    echo [ERROR] AudioCleanerPro.exe not found in %APP_DIR%.
    pause
    exit /b 1
)

if not exist "%RES_DIR%\words.txt" (
    echo [ERROR] words.txt missing in %RES_DIR%.
    pause
    exit /b 1
)

echo.
echo Build complete.
echo - EXE: %APP_DIR%\AudioCleanerPro.exe
echo - Resources: %RES_DIR%
dir /b /ad "%RES_DIR%\vosk-model*" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Vosk model not bundled. Place a vosk-model* folder into %RES_DIR%
) else (
    echo - Vosk model: bundled
)

echo.
echo To create an installer, open AudioCleanerProInstaller.iss in Inno Setup or run:
echo     iscc AudioCleanerProInstaller.iss
echo.
pause
endlocal
