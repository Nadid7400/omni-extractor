@echo off
echo Stopping OmniExtractor and ngrok...
taskkill /F /IM uvicorn.exe 2>nul
taskkill /F /IM ngrok.exe 2>nul
echo Stopped successfully.
pause
