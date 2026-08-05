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

rem ---------- 1. найти или установить Python ----------
call :find_python
if not defined PY (
    echo [1/3] Python не найден. Пробую установить через winget...
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        call :find_python
    )
)
if not defined PY (
    echo.
    echo   Не удалось поставить Python автоматически.
    echo   Установи вручную с https://python.org  ^(галочка "Add Python to PATH"^),
    echo   затем запусти этот файл ещё раз.
    start "" https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo [1/3] Python: !PY!
echo.

rem ---------- 2. зависимости Python ----------
echo [2/3] Ставлю зависимости ^(может занять пару минут^)...
"!PY!" -m pip install --upgrade pip
"!PY!" -m pip install -r requirements.txt
if !errorlevel! neq 0 (
    echo.
    echo   Ошибка установки зависимостей. Проверь интернет и запусти файл снова.
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
:find_python
set "PY="
rem py launcher -> полный путь к python.exe
for /f "delims=" %%i in ('py -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if defined PY goto :eof
rem python из PATH
for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if defined PY goto :eof
rem типичные пути (в т.ч. сразу после winget-установки, пока PATH не обновлён)
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
  "%ProgramFiles%\Python310\python.exe"
) do if exist %%P set "PY=%%~P"
goto :eof
