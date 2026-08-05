@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem  Запуск l2bot от администратора двойным кликом.
rem  Сам запрашивает права (UAC) и открывает окно бота (gui.py)
rem  без консольного окна (через pythonw).
rem  Python ищется несколькими способами — работает на любом ПК,
rem  где установлен Python 3.10+ (не завязан на один путь).
rem ============================================================

rem --- проверяем права администратора; если их нет — перезапуск с UAC ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem --- уже админ: запускаем GUI из папки этого файла ---
cd /d "%~dp0"

rem 0) ручной оверрайд: переменная окружения L2BOT_PYTHONW = путь к pythonw.exe
if defined L2BOT_PYTHONW if exist "%L2BOT_PYTHONW%" (
    start "" "%L2BOT_PYTHONW%" gui.py & exit /b
)

rem 1) Python Launcher (pyw) — ставится со стандартным Python, самый надёжный
where pyw >nul 2>&1
if %errorlevel%==0 ( start "" pyw gui.py & exit /b )

rem 2) pythonw из PATH (если Python добавлен в PATH при установке)
where pythonw >nul 2>&1
if %errorlevel%==0 ( start "" pythonw gui.py & exit /b )

rem 3) типичные пути установки Python (для пользователя и на весь ПК)
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
  "%ProgramFiles%\Python313\pythonw.exe"
  "%ProgramFiles%\Python312\pythonw.exe"
  "%ProgramFiles%\Python311\pythonw.exe"
  "%ProgramFiles%\Python310\pythonw.exe"
) do if exist %%P ( start "" %%P gui.py & exit /b )

rem --- не нашли Python ---
echo.
echo   Python (pythonw) НЕ НАЙДЕН.
echo   Установи Python 3.10-3.12 с https://python.org и при установке
echo   ОБЯЗАТЕЛЬНО отметь галочку "Add Python to PATH".
echo   Либо задай переменную окружения L2BOT_PYTHONW на свой pythonw.exe.
echo.
pause
exit /b
