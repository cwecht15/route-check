@echo off
rem  Route check - best route with live traffic between the hospital and home
rem  (the two addresses are HOSPITAL_ADDRESS / HOME_ADDRESS in .env).
rem  Double-click: pick a direction, then answer "leaving now or at a time?".
rem  Or run a direction straight from a shortcut / command line:
rem      route.bat                    menu (also writes a PDF and a map to reports\ and opens both)
rem      route.bat back               home -> hospital (still asks the time question)
rem      route.bat --now --pdf        hospital -> home, leaving now, no questions, PDF
rem      route.bat --now --map        same, with the interactive map (HTML) instead
rem      route.bat back --at "tomorrow 7am"
rem      route.bat --no-tolls --alts 3 --json
rem  Needs TOMTOM_API_KEY and the two addresses in the .env next to this file (README.md).
setlocal EnableExtensions
chcp 65001 >nul
title Route check
cd /d "%~dp0"

if "%~1"=="" goto menu
python route_check.py %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:menu
cls
echo.
echo   ROUTE CHECK   (TomTom live traffic)
echo   ------------------------------------------------------------------
echo    1  Hospital -^> Home
echo    2  Home -^> Hospital
echo    3  Quit
echo.
choice /c 123 /n /m "  Pick a number: "
if errorlevel 3 goto quit
set "DIR="
if errorlevel 2 set "DIR=back"
echo.
python route_check.py %DIR% --pdf --map
echo.
pause
goto menu

:quit
endlocal
exit /b 0
