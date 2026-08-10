@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title l2bot: обновление из GitHub

rem ============================================================
rem  Обновляет проект из GitHub (git pull) в ТЕКУЩЕЙ папке.
rem  Папку с ботом можно переносить куда угодно вместе с этим файлом:
rem  скрипт всегда работает в своей папке, а если привязка к git по новому
rem  пути потеряна (нет .git) — заново привязывает папку к GitHub здесь же.
rem ============================================================

set "REPO=https://github.com/Petrovalen/L2.git"

echo ============================================================
echo   Обновление l2bot из GitHub
echo   Папка: "%~dp0"
echo ============================================================
echo.

call :find_git
if not defined GIT (
    echo Git не найден. Установи Git или запусти УСТАНОВИТЬ_С_НУЛЯ.bat.
    pause
    exit /b 1
)

if not exist ".git" (
    echo Папка не привязана к git — привязываю к GitHub по ТЕКУЩЕМУ пути...
    "!GIT!" init -b main
    if errorlevel 1 "!GIT!" init
    "!GIT!" remote add origin "%REPO%" >nul 2>&1
    "!GIT!" remote set-url origin "%REPO%" >nul 2>&1
    echo Скачиваю из GitHub...
    "!GIT!" fetch origin
    if errorlevel 1 goto :neterr
    "!GIT!" reset --hard origin/main
    if errorlevel 1 goto :rebinderr
    "!GIT!" branch --set-upstream-to=origin/main >nul 2>&1
    goto :done
)

rem .git есть — убеждаемся, что origin указывает на наш репозиторий, и тянем.
"!GIT!" remote add origin "%REPO%" >nul 2>&1
"!GIT!" remote set-url origin "%REPO%" >nul 2>&1
echo Тяну свежую версию...
"!GIT!" pull --ff-only
if errorlevel 1 goto :pullerr

:done
echo.
echo ============================================================
echo   ГОТОВО. Проект привязан к git здесь: "%~dp0"
echo   Запуск бота:  l2bot\run_bot_admin.bat
echo ============================================================
echo.
pause
exit /b 0

:pullerr
echo.
echo   Обновиться по-быстрому не вышло — обычно локальные правки файлов проекта.
echo   Прокрути вывод выше, там причина. Настройки settings.json это не трогает.
pause
exit /b 1

:neterr
echo.
echo   Не удалось скачать из GitHub. Проверь интернет и повтори.
pause
exit /b 1

:rebinderr
echo.
echo   Не удалось привязать папку к git. Проще переустановить: удали .git и
echo   запусти УСТАНОВИТЬ_С_НУЛЯ.bat, либо пришли текст ошибки выше.
pause
exit /b 1

rem ==================== подпрограммы ====================
:find_git
set "GIT="
where git >nul 2>&1 && ( set "GIT=git" & goto :eof )
if exist "%ProgramFiles%\Git\cmd\git.exe" ( set "GIT=%ProgramFiles%\Git\cmd\git.exe" & goto :eof )
if exist "%ProgramFiles(x86)%\Git\cmd\git.exe" ( set "GIT=%ProgramFiles(x86)%\Git\cmd\git.exe" & goto :eof )
if exist "%LocalAppData%\Programs\Git\cmd\git.exe" ( set "GIT=%LocalAppData%\Programs\Git\cmd\git.exe" & goto :eof )
goto :eof
