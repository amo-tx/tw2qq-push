@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [tw2qq] QQ relay  :  http://127.0.0.1:5801/status
echo [tw2qq] NapCat    :  http://127.0.0.1:3000  (must be running with the alt account)
echo [tw2qq] Press Ctrl+C to stop.
python relay.py
pause
