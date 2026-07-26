@echo off
chcp 65001 >nul
title Audio Cleaner Pro v3.7

:: Переход в папку, где лежит этот батник (важно для корректной работы путей)
cd /d "%~dp0"

:: Проверка наличия Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ❌ Python не найден! Установите Python и добавьте его в PATH.
    pause
    exit /b 1
)

:: Запуск приложения
echo 🚀 Запуск Audio Cleaner Pro...
python app.py

:: Если приложение закрылось с ошибкой, окно не закроется сразу
if %errorlevel% neq 0 (
    echo.
    echo ⚠ Приложение завершилось с кодом ошибки: %errorlevel%
    pause
)