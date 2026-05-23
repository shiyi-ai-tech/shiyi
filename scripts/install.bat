@echo off
chcp 65001 >nul
echo ============================================
echo   史佚 ShiYi - 类人记忆 AI Agent 安装脚本
echo ============================================
echo.

:: 检查管理员权限
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请右键以管理员身份运行此脚本。
    pause
    exit /b 1
)

:: 检查 WSL
echo [1/4] 检查 WSL...
wsl --status >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 WSL，请先安装 WSL。
    echo.
    echo 以管理员身份打开 PowerShell，运行：
    echo   wsl --install
    echo 然后重启电脑，再运行此脚本。
    pause
    exit /b 1
)
echo   WSL 已就绪。

:: 安装目录
set "INSTALL_DIR=%USERPROFILE%\ShiYi"
set "SCRIPT_DIR=%~dp0"

echo [2/4] 复制文件到 %INSTALL_DIR%...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: 复制 .whl 包
copy /y "%SCRIPT_DIR%shiyi_common-*.whl" "%INSTALL_DIR%\" >nul 2>&1
copy /y "%SCRIPT_DIR%shiyi_core-*.whl"  "%INSTALL_DIR%\" >nul 2>&1
copy /y "%SCRIPT_DIR%shiyi_shell-*.whl"  "%INSTALL_DIR%\" >nul 2>&1

:: 复制安装脚本
copy /y "%SCRIPT_DIR%install.sh" "%INSTALL_DIR%\" >nul 2>&1
copy /y "%SCRIPT_DIR%install-windows-service.ps1" "%INSTALL_DIR%\" >nul 2>&1

echo   已复制。

:: 在 WSL 中执行安装
echo [3/4] 在 WSL 中安装 Python 包...
wsl bash "/mnt/c/Users/%USERNAME%/ShiYi/install.sh" "/mnt/c/Users/%USERNAME%/ShiYi/"
if %errorlevel% neq 0 (
    echo   ⚠ 安装过程有警告，请查看上方输出。
)

:: 注册开机自启
echo [4/4] 注册开机自启...
schtasks /create /tn "史佚ShiYi-WebUI" /tr "wsl bash -c 'nohup shiyi webui ^> /dev/null 2^>^&1 ^&'" /sc onlogon /delay 0000:30 /rl HIGHEST /f >nul 2>&1
if %errorlevel% equ 0 (
    echo   计划任务已注册，每次登录 30 秒后自动启动。
) else (
    echo   ⚠ 计划任务注册失败，请手动运行 install-windows-service.ps1
)

echo.
echo ============================================
echo   安装完成！
echo.
echo   使用方法：
echo     浏览器打开 http://localhost:8520
echo     首次使用需在设置页配置 API Key
echo.
echo   管理命令：
echo     wsl shiyi webui       手动启动
echo     wsl shiyi --version   查看版本
echo ============================================
echo.
echo 按任意键退出...
pause >nul
