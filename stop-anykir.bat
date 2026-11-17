@echo off
setlocal
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Stopping AnyKir...
echo.

sc query AnyKir >nul 2>&1
if not errorlevel 1 (
    echo  - Stopping the admin service
    net stop AnyKir >nul 2>&1
)

echo  - Ending AnyKir programs
taskkill /F /T /IM AnyKirSvc.exe >nul 2>&1
taskkill /F /T /IM AnyKir.exe >nul 2>&1

echo  - Freeing ports 7001 and 7002
for %%p in (7001 7002) do (
    for /f "tokens=5" %%i in ('netstat -ano ^| findstr /R /C:":%%p .*LISTENING"') do (
        if not "%%i"=="0" taskkill /F /T /PID %%i >nul 2>&1
    )
)

timeout /t 2 /nobreak >nul
echo.

set BUSY=
for %%p in (7001 7002) do (
    netstat -ano | findstr /R /C:":%%p .*LISTENING" >nul && set BUSY=1 && echo Port %%p is STILL IN USE:&& netstat -ano | findstr /R /C:":%%p .*LISTENING"
)
tasklist /FI "IMAGENAME eq AnyKir.exe" | find /I "AnyKir.exe" >nul && set BUSY=1 && echo AnyKir.exe is STILL RUNNING
tasklist /FI "IMAGENAME eq AnyKirSvc.exe" | find /I "AnyKirSvc.exe" >nul && set BUSY=1 && echo AnyKirSvc.exe is STILL RUNNING

if defined BUSY (
    echo.
    echo AnyKir is NOT fully stopped. Run this again, or restart the PC.
) else (
    echo AnyKir is fully stopped. Ports 7001 and 7002 are free.
)
echo.
echo The admin service starts again with Windows. AnyKir starts again with Windows if that is ticked.
echo.
pause
