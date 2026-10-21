# 视频分析 · DeepSeek —— 启动脚本
# 用法（在项目根目录）：
#   PowerShell -ExecutionPolicy Bypass -File .\run.ps1
#   .\run.ps1 -Port 8766 -NoBrowser
param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "未找到虚拟环境，请先运行: .\setup.ps1" -ForegroundColor Red
    exit 1
}

# 检查端口占用
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "端口 $Port 已被占用（PID: $($busy[0].OwningProcess)）。" -ForegroundColor Yellow
    Write-Host "可能已经在运行：直接打开 http://127.0.0.1:$Port" -ForegroundColor Yellow
    Write-Host "或换端口: .\run.ps1 -Port 8766" -ForegroundColor Yellow
    exit 1
}

$url = "http://127.0.0.1:$Port"
Write-Host "=== 视频分析 · DeepSeek ===" -ForegroundColor Cyan
Write-Host "服务地址: $url" -ForegroundColor Cyan
Write-Host "按 Ctrl+C 停止服务" -ForegroundColor DarkGray

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Seconds 3
        Start-Process $u
    } -ArgumentList $url | Out-Null
}

& $python -m vanalyze --host 127.0.0.1 --port $Port
