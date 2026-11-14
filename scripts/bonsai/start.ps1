<#
.SYNOPSIS
  Start llama-server with Bonsai 2 4B (Q2_0) on 127.0.0.1:8080 (OpenAI API at /v1).

.DESCRIPTION
  -Background (default) detaches the server, logs to <repo>\logs\bonsai-server-<port>.log, writes the PID
  to <repo>\logs\bonsai-server-<port>.pid and waits until /health is ok. -Foreground runs it in this console.
  One slot (-np 1) so cache_prompt + id_slot 0 always reuse the planner's static prefix.
  Run setup.ps1 first.

.EXAMPLE
  .\scripts\bonsai\start.ps1                     # Vulkan (iGPU), background, port 8080
  .\scripts\bonsai\start.ps1 -Backend cpu        # CPU only (no usable GPU)
  .\scripts\bonsai\start.ps1 -Foreground
#>
[CmdletBinding()]
param(
    # Vulkan default: on the Ryzen AI 7 350 / Radeon 860M box it processes prompts ~2.4x faster than
    # 8 CPU threads (74 vs 31 tok/s), and prompt processing is the planner's bottleneck.
    [ValidateSet('cpu', 'vulkan')] [string] $Backend = 'vulkan',
    [string] $Build = 'b12383',
    [int] $Port = 8080,
    [string] $BindHost = '127.0.0.1',
    [int] $Ctx = 8192,
    [int] $Threads = 0,
    [switch] $Foreground,
    [int] $WaitSeconds = 120
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Exe = Join-Path $Repo "models\llama.cpp\$Build\$Backend\llama-server.exe"
$Model = Join-Path $Repo 'models\bonsai\Ternary-Bonsai-2-4B-Q2_0.gguf'
$LogDir = Join-Path $Repo 'logs'
$Log = Join-Path $LogDir "bonsai-server-$Port.log"
$PidFile = Join-Path $LogDir "bonsai-server-$Port.pid"

foreach ($p in @($Exe, $Model)) { if (-not (Test-Path $p)) { throw "missing $p; run scripts\bonsai\setup.ps1" } }
if ($Threads -le 0) {
    # Physical cores: decode is memory-bound and SMT siblings only add contention.
    $Threads = [int]((Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum)
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "port $Port already in use (stop it with scripts\bonsai\stop.ps1 -Port $Port)"
}

$ServerArgs = @(
    '-m', $Model, '-c', $Ctx, '--jinja', '--host', $BindHost, '--port', $Port,
    '-t', $Threads, '-np', '1', '-fa', 'on', '--alias', 'bonsai', '--no-webui'
)
if ($Backend -eq 'vulkan') { $ServerArgs += @('-ngl', '99') } else { $ServerArgs += @('-ngl', '0') }

Write-Host "llama-server $Build ($Backend, $Threads threads, ctx $Ctx) -> http://${BindHost}:$Port/v1"
if ($Foreground) {
    & $Exe @ServerArgs
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force $LogDir | Out-Null
$proc = Start-Process -FilePath $Exe -ArgumentList $ServerArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$Log.out" -RedirectStandardError $Log
Set-Content -Encoding ascii -Path $PidFile -Value $proc.Id
Write-Host "pid $($proc.Id), log $Log"
& (Join-Path $PSScriptRoot 'health.ps1') -Port $Port -BindHost $BindHost -WaitSeconds $WaitSeconds
if ($LASTEXITCODE -ne 0) {
    Write-Host '--- last log lines ---'
    Get-Content -Tail 20 $Log -ErrorAction SilentlyContinue
}
exit $LASTEXITCODE
