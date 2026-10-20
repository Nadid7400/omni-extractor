Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
currentDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Change working directory to OmniExtractor
WshShell.CurrentDirectory = currentDir

' Start uvicorn server in background (0 = hidden window)
WshShell.Run "python -m uvicorn app:app --host 0.0.0.0 --port 8000", 0, False

' Give uvicorn 1.5 seconds to bind to port 8000
WScript.Sleep 1500

' Start permanent ngrok tunnel in background (0 = hidden window)
WshShell.Run "ngrok http 8000 --url https://tray-coherence-splurge.ngrok-free.dev", 0, False
