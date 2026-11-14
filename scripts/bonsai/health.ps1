<#
.SYNOPSIS
  Health check for the local Bonsai llama-server: /health ok, /v1/models lists a model, and a
  1-token chat completion with thinking off returns. Exit 0 = healthy.

.EXAMPLE
  .\scripts\bonsai\health.ps1
  .\scripts\bonsai\health.ps1 -WaitSeconds 120    # poll until healthy (used by start.ps1)
#>
[CmdletBinding()]
param(
    [int] $Port = 8080,
    [string] $BindHost = '127.0.0.1',
    [int] $WaitSeconds = 0
)
$ErrorActionPreference = 'Stop'
$Base = "http://${BindHost}:$Port"

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ($true) {
    try {
        $h = Invoke-RestMethod -TimeoutSec 3 "$Base/health"
        if ($h.status -eq 'ok') { break }
    } catch { $h = $null }
    if ((Get-Date) -ge $deadline) {
        Write-Host "UNHEALTHY: $Base/health not ok (is the server running? see logs\bonsai-server-$Port.log)"
        exit 1
    }
    Start-Sleep -Milliseconds 500
}

try {
    $models = Invoke-RestMethod -TimeoutSec 5 "$Base/v1/models"
    $id = $models.data[0].id
    $body = @{
        model = $id; max_tokens = 1; temperature = 0
        messages = @(@{ role = 'user'; content = 'Say OK.' })
        chat_template_kwargs = @{ enable_thinking = $false }
    } | ConvertTo-Json -Depth 5
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod -TimeoutSec 60 -Method Post -ContentType 'application/json' -Body $body "$Base/v1/chat/completions"
    $ms = $sw.ElapsedMilliseconds
} catch {
    Write-Host "UNHEALTHY: $($_.Exception.Message)"
    exit 1
}
Write-Host "HEALTHY: $Base/v1 model=$id 1-token chat ${ms} ms (prompt $($r.usage.prompt_tokens) tok)"
exit 0
