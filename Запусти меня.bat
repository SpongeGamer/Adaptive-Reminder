@echo off
setlocal
cd /d "%~dp0"

rem ------------------------------------------------------------
rem  Umnye Pamyatki - launcher
rem  This file is pure ASCII on purpose: cmd.exe reads .bat in the
rem  console code page (866 / 1251 / 65001 - differs per machine),
rem  so any Cyrillic here would turn into garbage and break parsing.
rem  All Russian output is printed by build.py instead.
rem ------------------------------------------------------------

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0build.py"
    goto done
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0build.py"
    goto done
)

echo.
echo ====================================================
echo   Python not found / Python ne nayden
echo.
echo   1. Install Python from https://python.org
echo   2. Tick "Add Python to PATH" during setup
echo   3. Run this file again
echo ====================================================
echo.

:done
echo.
pause
