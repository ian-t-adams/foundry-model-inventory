#requires -Version 5.1
<#
.SYNOPSIS
Runs Python -m dashboard collect from the repository, with an ignored UTF-8 log.
.DESCRIPTION
Used by the current-user morning task, independently of the localhost server.
All local data and logs stay below data. The Python collector owns concurrency,
timeouts, scope validation, and CSV ingestion; this wrapper preserves its exit code.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PythonPath,
    [string]$RepoRoot,
    [string]$DataDirectory
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$exitCode = 1
$writer = $null

function Protect-LogText([string]$Text) {
    $Text = $Text -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]'
    $Text = $Text -replace '(?i)(["'']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|authorization|api[_-]?key|connectionstring|device[_-]?code|user[_-]?code|sig)["'']?\s*[:=]\s*)("[^"]*"|''[^'']*''|[^\s,;&}]+)', '$1[REDACTED]'
    $Text -replace '\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]'
}

try {
    if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
    if (-not [IO.Path]::IsPathRooted($RepoRoot) -or $RepoRoot -match '["\r\n]') {
        throw 'RepoRoot must be an absolute local directory.'
    }
    $RepoRoot = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) { throw 'RepoRoot does not exist.' }
    if (-not $DataDirectory) { $DataDirectory = Join-Path $RepoRoot 'data' }
    if (-not [IO.Path]::IsPathRooted($DataDirectory) -or $DataDirectory -match '["\r\n]') {
        throw 'DataDirectory must be an absolute local directory.'
    }
    $DataDirectory = [IO.Path]::GetFullPath($DataDirectory).TrimEnd('\')
    $allowed = Join-Path $RepoRoot 'data'
    if ($DataDirectory -ine $allowed -and -not $DataDirectory.StartsWith($allowed + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'DataDirectory must remain under the repository data directory.'
    }
    if (-not [IO.Path]::IsPathRooted($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw 'PythonPath must point to an existing absolute Python executable.'
    }
    $PythonPath = [IO.Path]::GetFullPath($PythonPath)
    $logs = Join-Path $DataDirectory 'logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $log = Join-Path $logs ("morning-{0}-{1}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID)
    $writer = New-Object IO.StreamWriter($log, $false, (New-Object Text.UTF8Encoding($false)))
    $writer.AutoFlush = $true
    $writer.WriteLine("Scheduled collection started at " + (Get-Date).ToUniversalTime().ToString('o'))
    Push-Location -LiteralPath $RepoRoot
    try {
        $arguments = @('-m', 'dashboard', 'collect', '--data-dir', $DataDirectory, '--source', 'scheduled')
        # Native stderr is diagnostic output, not a terminating PS 5.1 ErrorRecord.
        $ErrorActionPreference = 'Continue'
        $PSNativeCommandUseErrorActionPreference = $false
        & $PythonPath @arguments 2>&1 | ForEach-Object {
            $safe = Protect-LogText ([string]$_)
            $writer.WriteLine($safe)
            Write-Output $safe
        }
        if ($null -ne $LASTEXITCODE) { $exitCode = [int]$LASTEXITCODE }
    } finally {
        $ErrorActionPreference = 'Stop'
        Pop-Location
    }
    $writer.WriteLine("Scheduled collection exited with code $exitCode")
} catch {
    $safe = Protect-LogText $_.Exception.Message
    if ($null -ne $writer) { $writer.WriteLine($safe) }
    [Console]::Error.WriteLine($safe)
    $exitCode = 1
} finally {
    if ($null -ne $writer) { $writer.Dispose() }
}
exit $exitCode
