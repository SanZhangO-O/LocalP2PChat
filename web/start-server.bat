@echo off
rem LocalChat Web server launcher.
rem Double-click this file: the server starts and the chat page opens in
rem your browser. Other users on the LAN open the http://<ip>:8090 address
rem shown in this window.
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js was not found. Please install Node.js 18 or newer from https://nodejs.org/ and run this file again.
  pause
  exit /b 1
)
node server\main.js --http-host 0.0.0.0 --open
pause
