@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title l2bot: установка с нуля (Git + проект + зависимости)

rem ============================================================
rem  Этот файл разворачивает l2bot на ЧИСТОМ компьютере одним запуском:
rem    1) ставит Git (если его нет),
rem    2) качает проект из GitHub,
rem    3) запускает штатный установщик (Python + пакеты + Tesseract).
rem  Скачай ТОЛЬКО этот файл (напр. на Рабочий стол) и запусти двойным кликом.
rem  Нужен интернет. Проект появится в папке L2 рядом с этим файлом.
rem ============================================================

set "REPO=https://github.com/Petrovalen/L2.git"
set "DEST=%~dp0L2"

echo ============================================================
echo   l2bot — установка с нуля на новом ПК
echo   Поставлю Git, скачаю проект и все зависимости.
echo   Проект будет здесь: "%DEST%"
echo ============================================================
echo.

rem ---------- 1. Git ----------
call :find_git
if not defined GIT (
    echo [1/3] Git не найден. Ставлю через winget...
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
        call :find_git
    ) else (
        echo   winget недоступен на этом ПК.
    )
)
if not defined GIT (
    echo.
    echo   Не удалось поставить Git автоматически.
    echo   Установи Git вручную ^(в установщике жми Next до конца^):
    echo     https://git-scm.com/download/win
    echo   затем запусти этот файл снова.
    start "" https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)
echo [1/3] Git: !GIT!
echo.

rem ---------- 2. проект из GitHub ----------
if exist "%DEST%\.git" (
    echo [2/3] Проект уже скачан — обновляю ^(git pull^)...
    "!GIT!" -C "%DEST%" pull --ff-only
) else (
    echo [2/3] Скачиваю проект в "%DEST%"...
    "!GIT!" clone "%REPO%" "%DEST%"
)
if !errorlevel! neq 0 (
    echo.
    echo   Не удалось скачать/обновить проект. Проверь интернет и повтори.
    pause
    exit /b 1
)
echo.

rem ---------- 3. зависимости (Python + пакеты + Tesseract) ----------
echo [3/3] Запускаю штатный установщик зависимостей проекта...
echo.
if exist "%DEST%\l2bot\УСТАНОВИТЬ.bat" (
    call "%DEST%\l2bot\УСТАНОВИТЬ.bat"
) else (
    echo   Не найден "%DEST%\l2bot\УСТАНОВИТЬ.bat" — структура репозитория изменилась?
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ГОТОВО! Проект: "%DEST%\l2bot"
echo   Запуск бота:   "%DEST%\l2bot\run_bot_admin.bat"
echo   Калибровка под этот ПК: см. "%DEST%\l2bot\ЗАПУСК_НА_ДРУГОМ_ПК.md"
echo ============================================================
echo.
pause
exit /b 0

rem ==================== подпрограммы ====================
:find_git
rem GIT = команда/путь запуска git, либо пусто. После winget-установки PATH в
rem текущем окне ещё не обновлён, поэтому дополнительно ищем git в стандартных папках.
set "GIT="
where git >nul 2>&1 && ( set "GIT=git" & goto :eof )
if exist "%ProgramFiles%\Git\cmd\git.exe" ( set "GIT=%ProgramFiles%\Git\cmd\git.exe" & goto :eof )
if exist "%ProgramFiles(x86)%\Git\cmd\git.exe" ( set "GIT=%ProgramFiles(x86)%\Git\cmd\git.exe" & goto :eof )
if exist "%LocalAppData%\Programs\Git\cmd\git.exe" ( set "GIT=%LocalAppData%\Programs\Git\cmd\git.exe" & goto :eof )
goto :eof
