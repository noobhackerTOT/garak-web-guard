$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot '.runtime\server.json'
if (-not (Test-Path -LiteralPath $statePath)) { throw '没有找到本地服务记录。' }
$serverState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$servicePort = [int]$serverState.port
if ($servicePort -lt 1 -or $servicePort -gt 65535) { throw '服务端口记录无效。' }
$baseUri = "http://127.0.0.1:$servicePort"
$response = Invoke-WebRequest -UseBasicParsing -Uri "$baseUri/" -TimeoutSec 10
$match = [regex]::Match($response.Content, 'name="console-token" content="([^"]+)"')
if (-not $match.Success) { throw '此端口不是 Garak Web Guard 控制台。' }
$headers = @{ 'X-Console-Token' = $match.Groups[1].Value }
Invoke-RestMethod -Method Post -Uri "$baseUri/api/shutdown" -Headers $headers -ContentType 'application/json' -Body '{}' | Out-Null
Write-Output '本地服务正在关闭，检测和独立浏览器会一并停止。'
