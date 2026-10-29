@echo off
rem  Route Check web server: a phone page that runs route_check.py and shows the map.
rem      serve.bat              share it over Tailscale (https://<this-pc>.<tailnet>.ts.net) and start
rem      serve.bat --lan        same, and also reachable on the home Wi-Fi (http://<pc-ip>:8000)
rem      serve.bat off          stop sharing over Tailscale, then exit
rem  Close this window (or Ctrl+C) to stop the server. README.md has the one-time Tailscale steps.
setlocal EnableExtensions
chcp 65001 >nul
title Route Check server
cd /d "%~dp0"

set "TS="
if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not defined TS for %%T in (tailscale.exe) do if not "%%~$PATH:T"=="" set "TS=%%~$PATH:T"

if /i "%~1"=="off" goto off

if not defined TS (
    echo Tailscale is not installed: only this PC can open http://127.0.0.1:8000
    echo.
    goto run
)

rem  Tailscale 1.46 and newer: "serve --bg 8000". Older: "serve https / http://127.0.0.1:8000".
set "TSVER="
set "TSMAJ=1"
set "TSMIN=99"
for /f "usebackq tokens=1,2 delims=." %%a in (`"%TS%" version 2^>nul`) do if not defined TSVER set "TSVER=%%a.%%b" & set "TSMAJ=%%a" & set "TSMIN=%%b"
set "NEWSERVE=1"
if defined TSVER if "%TSMAJ%"=="1" if %TSMIN% LSS 46 set "NEWSERVE="

echo Sharing http://127.0.0.1:8000 on your Tailscale network (Tailscale %TSVER%) ...
if defined NEWSERVE (
    "%TS%" serve --bg 8000
) else (
    "%TS%" serve https / http://127.0.0.1:8000
)
if errorlevel 1 (
    echo.
    echo Tailscale sharing did not start. Is Tailscale signed in, and are HTTPS certificates
    echo enabled on the DNS page of https://login.tailscale.com/admin ? The server still runs
    echo on this PC.
)
echo.
"%TS%" serve status
echo.

:run
python server.py %*
echo.
pause
exit /b 0

:off
if not defined TS (
    echo Tailscale is not installed; nothing to stop.
    exit /b 0
)
"%TS%" serve reset
echo Tailscale sharing stopped.
exit /b 0
