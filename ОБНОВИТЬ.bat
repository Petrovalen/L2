@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title l2bot: обновление из GitHub

rem ============================================================
rem  Обновляет уже установленный проект до свежей версии из GitHub.
rem  Положи этот файл В ПАПКУ ПРОЕКТА (там, где папка l2bot и .git) и
rem  запускай двойным кликом. Он делает git pull.
rem ============================================================

echo ============================================================
echo   Обновление l2bot из GitHub (git pull)
echo ============================================================
echo.

call :find_git
if not defined GIT (
    echo Git не найден. Установи Git или запусти УСТАНОВИТЬ_С_НУЛЯ.bat.
    pause
    exit /b 1
)
if not exist ".git" (
    echo Это не папка проекта из git - нет каталога .git. Запусти файл ИЗ папки,
    echo куда установщик скачал проект - там лежит папка l2bot.
    pause
    exit /b 1
)

echo Тяну свежую версию...
"!GIT!" pull --ff-only
if !errorlevel! neq 0 (
    echo.
    echo   Обновиться по-быстрому не вышло. Обычно причина — локальные правки
    echo   файлов проекта. Прокрути вывод выше, там причина.
    echo   Личные настройки settings.json это не затрагивает - они не в git.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ГОТОВО. Обновлено.
echo   Запуск бота:  l2bot\run_bot_admin.bat
echo   Если что-то не стартует — прогони l2bot\УСТАНОВИТЬ.bat (зависимости).
echo ============================================================
echo.
pause
exit /b 0

rem ==================== подпрограммы ====================
:find_git
set "GIT="
where git >nul 2>&1 && ( set "GIT=git" & goto :eof )
if exist "%ProgramFiles%\Git\cmd\git.exe" ( set "GIT=%ProgramFiles%\Git\cmd\git.exe" & goto :eof )
if exist "%ProgramFiles(x86)%\Git\cmd\git.exe" ( set "GIT=%ProgramFiles(x86)%\Git\cmd\git.exe" & goto :eof )
if exist "%LocalAppData%\Programs\Git\cmd\git.exe" ( set "GIT=%LocalAppData%\Programs\Git\cmd\git.exe" & goto :eof )
goto :eof
