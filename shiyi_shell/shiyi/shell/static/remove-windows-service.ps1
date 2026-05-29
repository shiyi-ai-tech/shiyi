# 史佚 ShiYi — Windows 后台服务卸载脚本
# Developed by LiGuo LeGang
# 功能：删除 Windows 计划任务，停止后台自启

param(
    [string]$TaskName = "史佚ShiYi-WebUI"
)

$ErrorActionPreference = "Continue"

Write-Host "正在停止史佚后台服务..." -ForegroundColor Yellow

# 停止 WSL 中运行的 shiyi 进程
wsl -- bash -c 'pkill -f "shiyi.webui" || true' 2>$null
Write-Host "已停止 WSL 中的史佚进程" -ForegroundColor Green

# 删除计划任务
schtasks /delete /tn $TaskName /f 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "已删除计划任务: $TaskName" -ForegroundColor Green
} else {
    Write-Host "计划任务不存在或已删除" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "✅ 史佚后台服务已完全卸载" -ForegroundColor Green
