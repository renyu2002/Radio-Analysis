# 视频分析 · DeepSeek —— 一键安装脚本
# 用法（在项目根目录）：
#   PowerShell -ExecutionPolicy Bypass -File .\setup.ps1
#   PowerShell -ExecutionPolicy Bypass -File .\setup.ps1 -Mirror   # 使用清华 PyPI 镜像
param(
    [switch]$Mirror,
    [switch]$SkipModel,
    [string]$Model = "large-v3"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "=== 视频分析 · DeepSeek 环境安装 ===" -ForegroundColor Cyan

# 1) Python
$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "[1/4] 创建虚拟环境 .venv ..." -ForegroundColor Yellow
    $sysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $sysPy) { throw "未找到 Python，请先安装 Python 3.12 并加入 PATH。" }
    & $sysPy -m venv .venv
} else {
    Write-Host "[1/4] 虚拟环境已存在，跳过创建" -ForegroundColor Green
}
& $python -m pip install --upgrade pip --quiet --disable-pip-version-check

# 2) 依赖
Write-Host "[2/4] 安装 Python 依赖（首次约 3-5 分钟，含 CUDA 运行库约 1.3GB）..." -ForegroundColor Yellow
$pipArgs = @("-r", "requirements.txt", "--disable-pip-version-check", "--no-input")
if ($Mirror) { $pipArgs += @("-i", "https://pypi.tuna.tsinghua.edu.cn/simple") }
& $python -m pip install @pipArgs
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败。可重试：.\setup.ps1 -Mirror" }

# 3) 目录
Write-Host "[3/4] 初始化数据目录 ..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\jobs"), (Join-Path $Root "data\uploads"), (Join-Path $Root "data\logs") | Out-Null

# 4) 可选：预下载 Whisper 模型
if (-not $SkipModel) {
    Write-Host "[4/4] 预下载 Whisper 模型 $Model（约 3GB，已下载会跳过）..." -ForegroundColor Yellow
    $env:WHISPER_MODEL_PRELOAD = $Model
    & $python -c @"
import os, sys
site = os.path.join(sys.prefix, 'Lib', 'site-packages', 'nvidia')
for sub in ('cudnn/bin','cublas/bin','cuda_runtime/bin','cuda_nvrtc/bin'):
    p = os.path.join(site, *sub.split('/'))
    if os.path.isdir(p):
        os.add_dll_directory(p)
        os.environ['PATH'] = p + os.pathsep + os.environ.get('PATH','')
from faster_whisper import WhisperModel
import ctranslate2
name = os.environ.get('WHISPER_MODEL_PRELOAD', 'large-v3')
device = 'cuda' if ctranslate2.get_cuda_device_count() > 0 else 'cpu'
ct = 'float16' if device == 'cuda' else 'int8'
print(f'下载/加载 {name} (device={device}, compute={ct}) ...')
WhisperModel(name, device=device, compute_type=ct)
print('模型就绪')
"@
} else {
    Write-Host "[4/4] 跳过模型预下载（首次分析时自动下载）" -ForegroundColor Green
}

Write-Host ""
Write-Host "=== 安装完成 ===" -ForegroundColor Green
Write-Host "启动应用:  .\run.ps1" -ForegroundColor Cyan
Write-Host "首次使用:  打开网页右上角「设置」，填入 DeepSeek API Key（https://platform.deepseek.com）" -ForegroundColor Cyan
Write-Host "Chrome 扩展: chrome://extensions -> 开发者模式 -> 加载已解压的扩展程序 -> 选择 extension 目录" -ForegroundColor Cyan
