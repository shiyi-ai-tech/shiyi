# 史佚 ShiYi — Windows 后台自启服务安装脚本
# Developed by LiGuo LeGang
# 
# 功能：注册 Windows 计划任务，用户登录后自动在 WSL 中启动史佚 WebUI
# 使用方法：在 Windows PowerShell 中以管理员权限运行此脚本
#   powershell -ExecutionPolicy Bypass -File install-windows-service.ps1
#
# 也可以右键 → "使用 PowerShell 运行"

param(
    [string]$TaskName = "史佚ShiYi-WebUI",
    [string]$WslDistro = "默认",   # WSL 发行版名称，留空=默认
    [string]$Port = "8520",
    [int]$DelaySeconds = 30
)

$ErrorActionPreference = "Stop"

# 检查管理员权限
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")) {
    Write-Host "[错误] 需要管理员权限。请右键 → 以管理员身份运行 PowerShell。" -ForegroundColor Red
    Write-Host "或者运行: Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy Bypass -File `"$PSCommandPath`"'" -ForegroundColor Yellow
    exit 1
}

Write-Host "╔════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║  史佚 ShiYi — 后台服务安装            ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# 构建 WSL 命令
if ($WslDistro -eq "默认" -or $WslDistro -eq "") {
    $WslPrefix = "wsl"
} else {
    $WslPrefix = "wsl -d $WslDistro"
}

# 动作：在 WSL 中启动 shiyi webui（自动加载 .env）
$Action = "$WslPrefix --cd /home/dolph -- bash -c 'nohup shiyi webui > /dev/null 2>&1 &'"

Write-Host "任务名称: $TaskName"
Write-Host "WSL 发行版: $(if ($WslDistro -eq '默认') {'默认'} else {$WslDistro})"
Write-Host "端口: $Port"
Write-Host "延迟: ${DelaySeconds}秒"
Write-Host ""

# 先删除旧任务（如果存在）
$existing = schtasks /query /tn $TaskName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[信息] 发现旧任务，正在删除..." -ForegroundColor Yellow
    schtasks /delete /tn $TaskName /f 2>$null | Out-Null
    Write-Host "[信息] 旧任务已删除" -ForegroundColor Green
}

# 创建计划任务
Write-Host "[信息] 正在创建计划任务..." -ForegroundColor Yellow
$result = schtasks /create `
    /tn $TaskName `
    /tr "$Action" `
    /sc onlogon `
    /delay "0000:${DelaySeconds}" `
    /rl highest `
    /f

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "╔════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║  ✅ 安装成功！                        ║" -ForegroundColor Green
    Write-Host "╚════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
    Write-Host "史佚将在每次 Windows 登录后 ${DelaySeconds} 秒自动启动。"
    Write-Host "打开浏览器访问: http://localhost:${Port}"
    Write-Host ""
    Write-Host "管理命令:"
    Write-Host "  手动启动: schtasks /run /tn `"$TaskName`""
    Write-Host "  手动停止: wsl -- bash -c 'pkill -f shiyi.webui'"
    Write-Host "  卸载服务: 运行 remove-windows-service.ps1"
    Write-Host ""
    Write-Host "提示：首次启动可能需要 10-30 秒等待 WSL 和 Python 就绪。"
} else {
    Write-Host ""
    Write-Host "[错误] 任务创建失败。请检查:"
    Write-Host "  - 是否以管理员身份运行"
    Write-Host "  - WSL 是否已安装 (wsl --status)"
    Write-Host "  - 史佚是否已在 WSL 中安装 (wsl -- shiyi --version)"
}

# 立即启动一次（可选）
Write-Host ""
$startNow = Read-Host "是否立即启动史佚？(y/n)"
if ($startNow -eq "y" -or $startNow -eq "Y") {
    Write-Host "[信息] 正在启动..." -ForegroundColor Yellow
    schtasks /run /tn $TaskName
    Write-Host "[信息] 已触发启动，请等待 10-30 秒后访问 http://localhost:${Port}" -ForegroundColor Green
}
