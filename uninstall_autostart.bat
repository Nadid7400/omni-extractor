@echo off
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_PATH=%STARTUP_DIR%\OmniExtractor.lnk

if exist "%SHORTCUT_PATH%" (
    del "%SHORTCUT_PATH%"
    echo [SUCCESS] OmniExtractor removed from Windows Startup.
) else (
    echo OmniExtractor was not found in Windows Startup.
)
pause
