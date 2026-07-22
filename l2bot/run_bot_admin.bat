@echo off
setlocal
rem ============================================================
rem  Запуск l2bot от администратора двойным кликом.
rem  Сам запрашивает права (UAC) и открывает окно бота (gui.py)
rem  без консольного окна (через pythonw).
rem ============================================================

rem --- проверяем права администратора; если их нет — перезапуск с UAC ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem --- уже админ: запускаем GUI из папки этого файла ---
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" gui.py
) else (
    start "" pythonw gui.py
)
exit /b
