<#
.SYNOPSIS
  Stop the background llama-server that start.ps1 launched on -Port (uses logs\bonsai-server-<port>.pid).
  Never touches servers it did not start.
#>
[CmdletBinding()]
param([int] $Port = 8080)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PidFile = Join-Path $Repo "logs\bonsai-server-$Port.pid"
if (-not (Test-Path $PidFile)) { Write-Host "no pid file for port $Port; nothing to stop"; exit 0 }
$serverPid = [int](Get-Content $PidFile)
$proc = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
if ($proc -and $proc.ProcessName -eq 'llama-server') {
    Stop-Process -Id $serverPid -Confirm:$false
    Write-Host "stopped llama-server pid $serverPid (port $Port)"
} else {
    Write-Host "pid $serverPid is not a running llama-server; removing stale pid file"
}
Remove-Item -Force $PidFile
