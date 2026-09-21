#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [string]$DataDirectory = (Join-Path $PSScriptRoot "data")
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$DataDirectory = [System.IO.Path]::GetFullPath($DataDirectory)
Push-Location $PSScriptRoot
try {
    & $python -m dashboard serve --port $Port --data-dir $DataDirectory
    if ($LASTEXITCODE -ne 0) { throw "Dashboard exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
