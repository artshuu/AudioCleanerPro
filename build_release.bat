@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo === AudioCleanerPro Release Build ===

echo Installing PyInstaller if needed...
python -m pip install --upgrade pip
python -m pip install pyinstaller

if exist build rd /s /q build
if exist dist rd /s /q dist

python -m PyInstaller --noconsole --clean --name AudioCleanerPro app.py
if errorlevel 1 (
  echo.
  echo [ERROR] PyInstaller build failed.
  pause
  exit /b 1
)

echo Copying runtime resources into dist\AudioCleanerPro...
if not exist dist\AudioCleanerPro (
  echo [ERROR] Expected dist\AudioCleanerPro not found.
  pause
  exit /b 1
)

copy /y "words.txt" "dist\AudioCleanerPro\" >nul
if exist "vosk-model-small-ru-0.22" (
    if exist dist\AudioCleanerPro\vosk-model-small-ru-0.22 rd /s /q dist\AudioCleanerPro\vosk-model-small-ru-0.22
    xcopy "vosk-model-small-ru-0.22" "dist\AudioCleanerPro\vosk-model-small-ru-0.22" /e /i /y >nul
) else (
    echo [WARNING] Model folder "vosk-model-small-ru-0.22" не найдена в корне проекта.
    echo Поместите папку модели рядом с EXE после сборки, чтобы приложение работало.
)

echo.
echo Build complete.
echo - EXE: dist\AudioCleanerPro\AudioCleanerPro.exe
if exist dist\AudioCleanerPro\vosk-model-small-ru-0.22 (
  echo - Vosk model copied to dist\AudioCleanerPro\vosk-model-small-ru-0.22
) else (
  echo [WARNING] Vosk model directory was not copied.
)

echo.
echo To create an installer, open AudioCleanerProInstaller.iss in Inno Setup or run:
 echo     iscc AudioCleanerProInstaller.iss

echo.
pause
endlocal
