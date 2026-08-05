@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title Установка l2bot

echo ============================================================
echo   Установка l2bot: Python + зависимости + Tesseract
echo   (двойной клик — и всё поставится; нужен интернет)
echo ============================================================
echo.

rem ---------- 1. найти/поставить СТАБИЛЬНЫЙ Python (3.10-3.12) ----------
rem Вызываем Python через launcher "py" (py -3.12 ...): он сам находит Python
rem по реестру, поэтому путь с кириллицей в имени пользователя не мешает.
rem Берём именно 3.10-3.12 — у них есть готовые сборки всех зависимостей
rem (у самых новых, напр. 3.13/3.14, колёс может ещё не быть).
call :find_stable
if not defined PYCMD (
    echo [1/3] Стабильный Python не найден. Ставлю Python 3.12 через winget...
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        call :find_stable
    )
)
if not defined PYCMD (
    echo.
    echo   Не удалось найти/поставить Python 3.10-3.12.
    echo   Установи Python 3.12 с https://python.org/downloads/release/python-3129/
    echo   ^(галочка "Add Python to PATH"^), затем запусти этот файл снова.
    start "" https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo [1/3] Python: !PYCMD!
echo.

rem ---------- 2. зависимости Python ----------
echo [2/3] Ставлю зависимости ^(может занять пару минут^)...
!PYCMD! -m pip install --upgrade pip
!PYCMD! -m pip install -r requirements.txt
if !errorlevel! neq 0 (
    echo.
    echo   Первая попытка не удалась — пробую в профиль пользователя ^(--user^)...
    !PYCMD! -m pip install --user -r requirements.txt
)
if !errorlevel! neq 0 (
    echo.
    echo   Не удалось поставить зависимости. ПРОКРУТИ ВЫШЕ и пришли
    echo   текст красной ошибки — по нему видно причину.
    pause
    exit /b 1
)
echo.

rem ---------- 3. Tesseract OCR ----------
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    echo [3/3] Tesseract уже установлен.
) else (
    echo [3/3] Tesseract не найден. Пробую установить через winget...
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install -e --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements
    ) else (
        echo   winget недоступен. Установи Tesseract вручную:
        echo     https://github.com/UB-Mannheim/tesseract/wiki
        start "" https://github.com/UB-Mannheim/tesseract/wiki
    )
)

echo.
echo ============================================================
echo   ГОТОВО!
echo   1^) Запусти бота: двойной клик по run_bot_admin.bat
echo   2^) Откалибруй под этот ПК ^(см. ЗАПУСК_НА_ДРУГОМ_ПК.md^)
echo ============================================================
echo.
pause
exit /b 0

rem ==================== подпрограммы ====================
:find_stable
rem PYCMD = команда запуска стабильного Python (py -3.12 / -3.11 / -3.10) или пусто.
set "PYCMD="
py -3.12 -c "" >nul 2>&1 && ( set "PYCMD=py -3.12" & goto :eof )
py -3.11 -c "" >nul 2>&1 && ( set "PYCMD=py -3.11" & goto :eof )
py -3.10 -c "" >nul 2>&1 && ( set "PYCMD=py -3.10" & goto :eof )
goto :eof
