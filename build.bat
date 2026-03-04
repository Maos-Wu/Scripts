@echo off
chcp 65001 >nul
echo ========================================
echo  帝江号集成终端 - PyInstaller 打包工具
echo ========================================
echo.

echo [1/3] 安装 PyInstaller...
pip install pyinstaller
if errorlevel 1 ( echo [错误] PyInstaller 安装失败 & pause & exit /b 1 )

echo.
echo [2/3] 清理旧构建...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo.
echo [3/3] 开始打包（约需 1-3 分钟）...
pyinstaller ^
  --onedir ^
  --name "EndfieldPanel" ^
  --add-data "templates;templates" ^
  --hidden-import=feedparser ^
  --hidden-import=charset_normalizer ^
  --hidden-import=charset_normalizer.md__mypyc ^
  --hidden-import=pkg_resources.py2_warn ^
  --hidden-import=multitasking ^
  --hidden-import=peewee ^
  --hidden-import=frozendict ^
  --hidden-import=appdirs ^
  --hidden-import=curl_cffi ^
  --hidden-import=curl_cffi.requests ^
  --collect-all yfinance ^
  --collect-all certifi ^
  --copy-metadata yfinance ^
  stock_monitor_ui.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请查看上方错误信息
    pause
    exit /b 1
)

echo.
echo [4/4] 生成启动脚本...
(
    echo @echo off
    echo chcp 65001 ^>nul
    echo echo 启动帝江号集成终端...
    echo echo 正在启动，请稍候 3 秒后浏览器将自动打开
    echo echo 若浏览器未打开，请手动访问: http://localhost:5000
    echo echo 关闭此窗口即可停止服务
    echo echo.
    echo start "" "EndfieldPanel.exe"
    echo timeout /t 3 /nobreak ^>nul
    echo start "" http://localhost:5000
) > "dist\EndfieldPanel\启动.bat"

echo.
echo ========================================
echo  打包完成！
echo  输出目录: dist\EndfieldPanel\
echo  发送时将整个 dist\EndfieldPanel\ 文件夹打包发送
echo  对方双击【启动.bat】即可运行（会自动打开浏览器）
echo ========================================
pause
