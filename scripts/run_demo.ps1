<#
.SYNOPSIS
  One-command demo launcher for the Windows dev box (twin of scripts/run_demo.sh for the Mac).

.DESCRIPTION
  start  : Bonsai on :8080 (scripts\bonsai\start.ps1, reused if already healthy), then the brain on
           :8765 with fixtures (--fixture finder; no Mac provider here). Waits for /health with
           bonsai:true and for the "planner warm" log line, then prints the console URL, the
           rehearsal command and an OFFLINE check (exit 3 if the internet is reachable).
  status : health of Bonsai + brain, plus the offline check.
  stop   : stops only what this script started (state in logs\run_demo.state).
  Logs/pids: logs\brain.log (server log), logs\brain.out, logs\brain.pid.

.EXAMPLE
  .\scripts\run_demo.ps1
  .\scripts\run_demo.ps1 -AllowOnline          # dev box with wifi on: offline check only warns
  .\scripts\run_demo.ps1 status
  .\scripts\run_demo.ps1 stop
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'status', 'stop')] [string] $Action = 'start',
    [string] $Fixture = 'finder',
    [int] $BrainPort = 8765,
    [int] $BonsaiPort = 8080,
    [int] $WaitSeconds = 180,
    [switch] $AllowOnline
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Repo 'logs'
$State = Join-Path $Logs 'run_demo.state'
$BrainLog = Join-Path $Logs 'brain.log'
$BrainOut = Join-Path $Logs 'brain.out'
$BrainPid = Join-Path $Logs 'brain.pid'
$BrainUrl = "http://127.0.0.1:$BrainPort"
New-Item -ItemType Directory -Force $Logs | Out-Null

function Test-Started([string] $What) { (Test-Path $State) -and ((Get-Content $State) -contains $What) }
function Set-Started([string] $What) { if (-not (Test-Started $What)) { Add-Content -Encoding ascii $State $What } }

function Get-Json([string] $Url) {
    try { return Invoke-RestMethod -TimeoutSec 2 $Url } catch { return $null }
}

function Wait-Until([string] $What, [scriptblock] $Check) {
    for ($i = 0; $i -lt $WaitSeconds; $i++) {
        if (& $Check) { Write-Host "  ok: $What (${i}s)"; return $true }
        Start-Sleep -Seconds 1
    }
    Write-Host "  FAILED: $What not ready after ${WaitSeconds}s"
    return $false
}

function Start-Bonsai {
    Write-Host "== Bonsai on :$BonsaiPort"
    $h = Get-Json "http://127.0.0.1:$BonsaiPort/health"
    if ($h -and $h.status -eq 'ok') { Write-Host '  already healthy: reusing (stop will not stop it)'; return }
    & (Join-Path $PSScriptRoot 'bonsai\start.ps1') -Port $BonsaiPort
    if ($LASTEXITCODE -ne 0) { throw "Bonsai did not start (see logs\bonsai-server-$BonsaiPort.log)" }
    Set-Started 'bonsai'
}

function Start-Brain {
    Write-Host "== Brain on $BrainUrl (fixture $Fixture)"
    if (Get-Json "$BrainUrl/health") { Write-Host '  already running: reusing (stop will not stop it)'; return }
    $py = Join-Path $Repo '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { throw "missing $py; run 'uv sync' first" }
    $argList = @('-m', 'brain.server', '--fixture', $Fixture, '--port', $BrainPort)
    # Python logging goes to stderr, so the server log is the stderr stream.
    $proc = Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $Repo -WindowStyle Hidden `
        -PassThru -RedirectStandardError $BrainLog -RedirectStandardOutput $BrainOut
    Set-Content -Encoding ascii $BrainPid $proc.Id
    Set-Started 'brain'
    Write-Host "  started pid $($proc.Id), log $BrainLog"
    if (-not (Wait-Until 'brain /health' { $null -ne (Get-Json "$BrainUrl/health") })) {
        Get-Content -Tail 20 $BrainLog -ErrorAction SilentlyContinue; throw 'brain did not come up'
    }
    if (-not (Wait-Until 'brain sees Bonsai (bonsai:true)' { (Get-Json "$BrainUrl/health").bonsai -eq $true })) {
        throw "brain cannot reach Bonsai :$BonsaiPort"
    }
    $warm = { Select-String -Quiet -Path $BrainLog -Pattern 'planner warm' -ErrorAction SilentlyContinue }
    [void](Wait-Until 'planner warmup finished' $warm)
    $line = Select-String -Path $BrainLog -Pattern 'planner warm \(\d+ ms\)|planner warmup failed' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($line) { Write-Host "  $($line.Line.Trim())" } else { Write-Host "  WARNING: no 'planner warm' line yet" }
}

function Test-Offline {
    Write-Host '== Offline check'
    & curl.exe -s -o NUL -m 3 https://example.com
    if ($LASTEXITCODE -eq 0) {
        Write-Host ''
        Write-Host '  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!' -ForegroundColor Red
        Write-Host '  !!  WIFI IS ON: https://example.com is reachable.         !!' -ForegroundColor Red
        Write-Host '  !!  The demo must run offline. Turn wifi off and re-check. !!' -ForegroundColor Red
        Write-Host '  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!' -ForegroundColor Red
        if ($AllowOnline) { Write-Host '  (-AllowOnline: continuing)'; return 0 }
        return 3
    }
    Write-Host '  ok: internet unreachable (offline)'
    return 0
}

function Show-Banner {
    Write-Host ''
    Write-Host "Console:    $BrainUrl/console"
    Write-Host 'Rehearse:   uv run python scripts/demo/rehearse.py --live'
    Write-Host 'Events:     uv run python -m brain.cli --watch --history 20'
    Write-Host 'Stop:       .\scripts\run_demo.ps1 stop'
}

function Stop-Demo {
    if ((Test-Started 'brain') -and (Test-Path $BrainPid)) {
        $id = [int](Get-Content $BrainPid)
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($p) { Stop-Process -Id $id -Confirm:$false; Write-Host "stopped brain (pid $id)" }
        else { Write-Host "brain pid $id already gone" }
        Remove-Item -Force $BrainPid
    } else { Write-Host 'brain: not started by run_demo; leaving it' }
    if (Test-Started 'bonsai') { & (Join-Path $PSScriptRoot 'bonsai\stop.ps1') -Port $BonsaiPort }
    else { Write-Host 'bonsai: not started by run_demo; leaving it' }
    if (Test-Path $State) { Remove-Item -Force $State }
}

switch ($Action) {
    'start' { Start-Bonsai; Start-Brain; Show-Banner; exit (Test-Offline) }
    'status' {
        $rc = 0
        $b = Get-Json "http://127.0.0.1:$BonsaiPort/health"
        Write-Host "== Bonsai :$BonsaiPort -> $(if ($b) { $b.status } else { 'DOWN' })"
        if (-not $b) { $rc = 1 }
        $h = Get-Json "$BrainUrl/health"
        Write-Host "== Brain $BrainUrl -> $(if ($h) { $h | ConvertTo-Json -Compress } else { 'DOWN' })"
        if (-not $h -or $h.bonsai -ne $true) { $rc = 1 }
        $o = Test-Offline
        if ($o -ne 0) { $rc = $o }
        exit $rc
    }
    'stop' { Stop-Demo }
}
