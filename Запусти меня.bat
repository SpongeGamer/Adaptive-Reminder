@echo off
chcp 65001 >nul
title Сборка "Умные Памятки"

echo ==========================================
echo   Умные Памятки - сборка EXE
echo ==========================================
echo.

:: --- Проверяем, что Python вообще установлен ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден.
    echo Установи Python с python.org и обязательно поставь
    echo галочку "Add Python to PATH" при установке.
    echo.
    pause
    exit /b 1
)

echo [1/4] Ставим зависимости...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install PySide6 pyinstaller
if errorlevel 1 (
    echo [ОШИБКА] Не удалось поставить зависимости.
    echo Проверь интернет и попробуй снова.
    pause
    exit /b 1
)

echo [2/4] Чистим прошлую сборку...
if exist dist rmdir /s /q dist >nul 2>&1
if exist build rmdir /s /q build >nul 2>&1
if exist "Умные Памятки.spec" del /q "Умные Памятки.spec" >nul 2>&1

:: --- Ресурсы подключаем, только если они реально лежат рядом ---
set EXTRA=
if exist icon.ico set EXTRA=%EXTRA% --icon=icon.ico --add-data "icon.ico;."
if exist notification.wav set EXTRA=%EXTRA% --add-data "notification.wav;."

if not exist icon.ico echo    [i] icon.ico не найден - иконка будет нарисована программой
if not exist notification.wav echo    [i] notification.wav не найден - будет системный звук

echo [3/4] Собираем EXE (это займёт пару минут)...
pyinstaller --onefile --windowed --noconsole --clean ^
    --name "Умные Памятки" %EXTRA% adaptive_reminder.py

if not exist "dist\Умные Памятки.exe" (
    echo.
    echo [ОШИБКА] EXE не собрался. Вывод PyInstaller выше - посмотри последние строки.
    pause
    exit /b 1
)

echo [4/4] Делаем ярлык на рабочем столе...
powershell -NoProfile -Command ^
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Умные Памятки.lnk');" ^
    "$s.TargetPath='%cd%\dist\Умные Памятки.exe';" ^
    "$s.WorkingDirectory='%cd%\dist';" ^
    "if (Test-Path '%cd%\icon.ico') { $s.IconLocation='%cd%\icon.ico' };" ^
    "$s.Description='Умные напоминания и планировщик';" ^
    "$s.Save()" >nul 2>&1

echo.
echo ==========================================
echo   Готово!
echo   EXE:   dist\Умные Памятки.exe
echo   Ярлык: на рабочем столе
echo.
echo   Напоминания хранятся в:
echo   %%APPDATA%%\AdaptiveReminder
echo   (не в папке с программой - их не потеряешь
echo    при пересборке или переносе)
echo ==========================================
echo.
pause
