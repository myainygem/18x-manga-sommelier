@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动推荐 UI ...
start "" "http://127.0.0.1:8765/"
".venv\Scripts\python.exe" cli.py serve --no-browser
pause
