@echo off
chcp 65001 >nul
echo 启动帝江号集成终端...
echo 浏览器访问: http://localhost:5000
echo 按 Ctrl+C 可停止服务
echo.
start "" http://localhost:5000
python stock_monitor_ui.py
pause
