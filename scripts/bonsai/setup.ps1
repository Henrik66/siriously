<#
.SYNOPSIS
  Download Bonsai 2 4B (Q2_0 GGUF) and a stock llama.cpp Windows build for the Siriously planner.

.DESCRIPTION
  Idempotent: files already present with the right SHA-256 are skipped.
  Layout (all under <repo>\models, which is gitignored):
    models\bonsai\Ternary-Bonsai-2-4B-Q2_0.gguf
    models\llama.cpp\<build>\cpu\llama-server.exe
    models\llama.cpp\<build>\vulkan\llama-server.exe
  Q2_0 needs upstream llama.cpp >= b11556; older builds load it but emit garbage silently.

.EXAMPLE
  .\scripts\bonsai\setup.ps1                 # model + CPU + Vulkan builds
  .\scripts\bonsai\setup.ps1 -Backend cpu    # model + CPU build only
#>
[CmdletBinding()]
param(
    [ValidateSet('cpu', 'vulkan', 'both')] [string] $Backend = 'both',
    [string] $Build = 'b12383',
    [string] $ModelsDir = ''
)
$ErrorActionPreference = 'Stop'
# PS 5.1 does not set $PSScriptRoot inside param() defaults, so resolve here.
if (-not $ModelsDir) { $ModelsDir = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'models' }
$ProgressPreference = 'SilentlyContinue'

$ModelRepo = 'prism-ml/Ternary-Bonsai-2-4B-gguf'
$ModelFile = 'Ternary-Bonsai-2-4B-Q2_0.gguf'
$ModelSha256 = '64a5183b44921bdb5ac4a6f5f92007821d6ebe5c66997d3c147fd214ab9433d4'
$MinBuild = 11556

function Get-Download([string] $Url, [string] $Dest) {
    $tmp = "$Dest.part"
    Write-Host "  GET $Url"
    & curl.exe -L --fail --retry 3 --continue-at - -o $tmp $Url
    if ($LASTEXITCODE -ne 0) { throw "download failed ($LASTEXITCODE): $Url" }
    Move-Item -Force $tmp $Dest
}

function Test-Sha256([string] $Path, [string] $Expected) {
    (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLowerInvariant() -eq $Expected
}

function Install-Model {
    $dir = Join-Path $ModelsDir 'bonsai'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $dest = Join-Path $dir $ModelFile
    if ((Test-Path $dest) -and (Test-Sha256 $dest $ModelSha256)) {
        Write-Host "model ok: $dest"
        return
    }
    Get-Download "https://huggingface.co/$ModelRepo/resolve/main/$ModelFile" $dest
    if (-not (Test-Sha256 $dest $ModelSha256)) {
        Remove-Item -Force $dest
        throw "SHA-256 mismatch for $ModelFile (deleted); re-run setup"
    }
    Write-Host "model ok: $dest"
}

function Install-LlamaCpp([string] $Kind) {
    $dir = Join-Path $ModelsDir "llama.cpp\$Build\$Kind"
    $exe = Join-Path $dir 'llama-server.exe'
    if (Test-Path $exe) {
        Write-Host "llama.cpp $Build ($Kind) ok: $exe"
        return
    }
    $zipName = "llama-$Build-bin-win-$Kind-x64.zip"
    $zip = Join-Path $env:TEMP $zipName
    Get-Download "https://github.com/ggml-org/llama.cpp/releases/download/$Build/$zipName" $zip
    New-Item -ItemType Directory -Force $dir | Out-Null
    Expand-Archive -Force -Path $zip -DestinationPath $dir
    Remove-Item -Force $zip
    # Some releases nest the binaries one level down; flatten so $exe is stable.
    if (-not (Test-Path $exe)) {
        $found = Get-ChildItem -Recurse -Filter 'llama-server.exe' $dir | Select-Object -First 1
        if (-not $found) { throw "llama-server.exe not found in $zipName" }
        Get-ChildItem $found.DirectoryName | Move-Item -Destination $dir -Force
    }
    Write-Host "llama.cpp $Build ($Kind) ok: $exe"
}

if ([int]($Build.TrimStart('b')) -lt $MinBuild) { throw "Q2_0 needs llama.cpp >= b$MinBuild (got $Build)" }
Install-Model
$kinds = if ($Backend -eq 'both') { @('cpu', 'vulkan') } else { @($Backend) }
foreach ($k in $kinds) { Install-LlamaCpp $k }
Write-Host "setup done. Start with: .\scripts\bonsai\start.ps1"
