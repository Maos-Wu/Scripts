@echo off
chcp 65001 >nul
echo 正在检查 Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10 及以上版本
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo 正在安装依赖包...
pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接
    pause
    exit /b 1
)
echo.
echo 安装完成！请运行 启动.bat
pause
