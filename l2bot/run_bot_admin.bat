@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem  Запуск l2bot от администратора двойным кликом.
rem  Сам запрашивает права (UAC) и открывает окно бота (gui.py)
rem  без консольного окна.
rem  Python вызывается через launcher (pyw -3.12 ...): он сам находит
rem  Python по реестру, поэтому кириллица в имени пользователя не мешает.
rem  Берём ту же стабильную версию (3.10-3.12), что и установщик.
rem ============================================================

rem --- проверяем права администратора; если их нет — перезапуск с UAC ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem --- уже админ: запускаем GUI из папки этого файла ---
cd /d "%~dp0"

rem подобрать команду запуска (windowless) стабильного Python
set "PYW="
py -3.12 -c "" >nul 2>&1 && set "PYW=pyw -3.12"
if not defined PYW ( py -3.11 -c "" >nul 2>&1 && set "PYW=pyw -3.11" )
if not defined PYW ( py -3.10 -c "" >nul 2>&1 && set "PYW=pyw -3.10" )
if not defined PYW ( py -c "" >nul 2>&1 && set "PYW=pyw" )
if not defined PYW ( where pythonw >nul 2>&1 && set "PYW=pythonw" )

if defined PYW (
    start "" %PYW% gui.py
    exit /b
)

echo.
echo   Python не найден. Сначала запусти УСТАНОВИТЬ.bat
echo   (или установи Python 3.12 с https://python.org, галочка "Add Python to PATH").
echo.
pause
exit /b
