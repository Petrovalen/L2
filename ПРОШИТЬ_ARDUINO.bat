@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title l2bot: перепрошивка Arduino (HID-мост)

rem ============================================================
rem  Перепрошивает Arduino (HID-мост) свежей прошивкой из проекта.
rem  Сам находит или СКАЧИВАЕТ arduino-cli, ставит ядро AVR и библиотеки
rem  Keyboard/Mouse, определяет COM-порт и заливает скетч.
rem
rem  ВАЖНО: СНАЧАЛА ЗАКРОЙ БОТА (окно панели) — иначе COM-порт занят и
rem  заливка не пройдёт. Нужен интернет (первый раз качает arduino-cli/ядро).
rem
rem  Если порт не определяется сам — запусти с портом в аргументе:
rem     ПРОШИТЬ_ARDUINO.bat COM7
rem ============================================================

set "SKETCH=%~dp0l2bot\arduino\l2bot_hid"
set "FQBN=arduino:avr:micro"
set "PORT=%~1"

echo ============================================================
echo   Перепрошивка Arduino (HID-мост l2bot)
echo   ЗАКРОЙ БОТА перед прошивкой! (иначе порт занят)
echo ============================================================
echo.
pause
echo.

if not exist "%SKETCH%\l2bot_hid.ino" (
    echo Не найден скетч: "%SKETCH%\l2bot_hid.ino"
    echo Запусти этот файл ИЗ папки проекта (рядом должна быть папка l2bot).
    pause
    exit /b 1
)

rem ---------- 1) найти или скачать arduino-cli ----------
set "ACLI="
if exist "C:\AI\tools\arduino-cli\arduino-cli.exe" set "ACLI=C:\AI\tools\arduino-cli\arduino-cli.exe"
if not defined ACLI if exist "%~dp0tools\arduino-cli\arduino-cli.exe" set "ACLI=%~dp0tools\arduino-cli\arduino-cli.exe"
if not defined ACLI ( where arduino-cli >nul 2>&1 && set "ACLI=arduino-cli" )
if not defined ACLI (
    echo arduino-cli не найден — скачиваю ^(один раз^)...
    set "ACDIR=%~dp0tools\arduino-cli"
    if not exist "!ACDIR!" mkdir "!ACDIR!"
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip' -OutFile '!ACDIR!\acli.zip'; Expand-Archive -Force '!ACDIR!\acli.zip' '!ACDIR!'; exit 0 } catch { exit 1 }"
    if exist "!ACDIR!\arduino-cli.exe" set "ACLI=!ACDIR!\arduino-cli.exe"
)
if not defined ACLI (
    echo Не удалось получить arduino-cli. Проверь интернет или прошей через Arduino IDE.
    pause
    exit /b 1
)
echo arduino-cli: !ACLI!
echo.

rem ---------- 2) ядро AVR + библиотеки (идемпотентно) ----------
echo Проверяю ядро arduino:avr и библиотеки Keyboard/Mouse...
"!ACLI!" core update-index >nul 2>&1
"!ACLI!" core install arduino:avr
"!ACLI!" lib install Keyboard Mouse
echo.

rem ---------- 3) определить COM-порт ----------
if not defined PORT (
    "!ACLI!" board list > "%TEMP%\l2_ports.txt" 2>&1
    for /f "tokens=1" %%p in ('findstr /i "arduino:avr:micro" "%TEMP%\l2_ports.txt"') do set "PORT=%%p"
    if not defined PORT (
        for /f "tokens=1" %%p in ('findstr /i "Micro" "%TEMP%\l2_ports.txt"') do set "PORT=%%p"
    )
)
if not defined PORT (
    echo Не нашёл порт Arduino. Подключённые порты:
    "!ACLI!" board list
    echo.
    echo Запусти файл с портом в аргументе, напр:  ПРОШИТЬ_ARDUINO.bat COM7
    pause
    exit /b 1
)
echo Порт Arduino: !PORT!
echo.

rem ---------- 4) компиляция + заливка ----------
echo Компилирую и заливаю прошивку...
"!ACLI!" compile --fqbn %FQBN% --upload -p !PORT! "%SKETCH%"
if !errorlevel! neq 0 (
    echo.
    echo   Не удалось залить. Частые причины:
    echo     - БОТ ЗАПУЩЕН и держит порт !PORT! -^> закрой бота и повтори;
    echo     - неверный порт -^> запусти с портом в аргументе (см. список выше).
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ГОТОВО. Arduino перепрошита.
echo   Запускай бота: l2bot\run_bot_admin.bat
echo ============================================================
echo.
pause
exit /b 0
