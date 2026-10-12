param(
    [int]$Port = 8787,
    [string]$GarakPython = 'D:\garak\venv\Scripts\python.exe',
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath $GarakPython)) {
    throw '找不到 garak Python，请通过 -GarakPython 指定已有 garak 环境。'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.vendor\playwright\__init__.py'))) {
    throw '尚未安装浏览器依赖，请先运行 Install.ps1。'
}
$launchArgs = @('app.py', '--port', "$Port")
if (-not $NoOpen) { $launchArgs += '--open' }
& $GarakPython @launchArgs
