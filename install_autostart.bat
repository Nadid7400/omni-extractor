@echo off
set SCRIPT_PATH=%~dp0launch_background.vbs
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_PATH=%STARTUP_DIR%\OmniExtractor.lnk

echo Creating startup shortcut...
powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath='wscript.exe'; $s.Arguments='\"%SCRIPT_PATH%\"'; $s.WorkingDirectory='%~dp0'; $s.Save()"

if exist "%SHORTCUT_PATH%" (
    echo [SUCCESS] OmniExtractor has been added to Windows Startup!
    echo It will now run silently in the background whenever you boot your PC.
) else (
    echo [ERROR] Could not create shortcut.
)
pause
