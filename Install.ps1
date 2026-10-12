param(
    [string]$GarakPython = 'D:\garak\venv\Scripts\python.exe',
    [string]$IndexUrl = 'https://pypi.tuna.tsinghua.edu.cn/simple',
    [switch]$Chromium
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$dependencyArgs = @('pip','install','--cache-dir',(Join-Path $PSScriptRoot '.runtime\uv-cache'),
    '--python',$GarakPython,'--target',(Join-Path $PSScriptRoot '.vendor'),
    '--index-url',$IndexUrl,'-r',(Join-Path $PSScriptRoot 'requirements-browser.txt'))
& uv @dependencyArgs
if ($LASTEXITCODE -ne 0) { throw 'Playwright 依赖安装失败' }
if ($Chromium) {
    $env:PYTHONPATH = Join-Path $PSScriptRoot '.vendor'
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.runtime\browsers'
    & $GarakPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Chromium 安装失败' }
}
Write-Output '依赖已安装。默认使用本机 Edge，运行 Start.ps1 即可。'
