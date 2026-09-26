@echo off
rem LocalChat unified server launcher (web chat + signaling/relay in one
rem Python program). Double-click this file: the server starts and the chat
rem page opens in your browser. Other users on the LAN open the
rem http://<ip>:8090 address shown in this window.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.9+ was not found. Please install Python from https://www.python.org/ and run this file again.
  pause
  exit /b 1
)
python -c "import cryptography" >nul 2>nul
if errorlevel 1 (
  echo Missing dependency "cryptography". Run:  python -m pip install cryptography
  pause
  exit /b 1
)
python "..\server\localchat_server.py" --http-host 0.0.0.0 --open
pause
