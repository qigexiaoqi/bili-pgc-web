@echo off
rem ---------------------------------------------------------------
rem  web.bat - start the local web UI for the Bilibili PGC downloader
rem  ASCII only on purpose (Chinese text in a .bat breaks cmd parsing).
rem
rem  Default: listen on 0.0.0.0 so your phone can reach it on the LAN.
rem  Pass --host 127.0.0.1 if you really want this PC only.
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal enabledelayedexpansion
title Bilibili PGC Downloader - Web
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HERE=%~dp0"
cd /d "%HERE%"

set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    python3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python3"
)
if not defined PY (
    echo [!] Python not found. Trying to install Python 3.12 via winget...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [x] Python still not found: https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

rem ---------- LAN by default, unless the caller already passed --host ----------
set "ARGS=%*"
echo %ARGS% | findstr /i /c:"--host" >nul
if errorlevel 1 set "ARGS=%ARGS% --host 0.0.0.0"

rem ---------- firewall: let inbound TCP 8737 through (needs admin) ----------
netsh advfirewall firewall show rule name="bili-pgc-web" >nul 2>nul
if errorlevel 1 (
    netsh advfirewall firewall add rule name="bili-pgc-web" dir=in action=allow protocol=TCP localport=8737 >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   [!] Could not add the firewall rule automatically.
        echo       If your phone cannot open the page, run this once in an ADMIN cmd:
        echo       netsh advfirewall firewall add rule name="bili-pgc-web" dir=in action=allow protocol=TCP localport=8737
    ) else (
        echo   [i] Firewall rule added for TCP 8737.
    )
)

echo.
echo   Local  : http://127.0.0.1:8737/
echo   Phone  : open the "LAN" address printed below, on the same Wi-Fi
echo   Test   : http://LAN-IP:8737/healthz  should answer "ok"
echo.

%PY% "%HERE%server.py" %ARGS%

echo.
pause
