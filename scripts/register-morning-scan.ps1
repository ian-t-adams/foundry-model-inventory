#requires -Version 5.1
<#
.SYNOPSIS
Queries or manages only FoundryModelInventory-MorningScan for the current user.
.DESCRIPTION
Use -Enabled -Time 07:00 -PythonPath <absolute python.exe> to register the task.
Use -Disable to disable it, or -Status (the default) for read-only JSON status.
Requires no elevation or saved password. You must be signed into Windows and
Azure CLI authentication must remain valid. The daily time is local;
StartWhenAvailable starts missed runs when the machine becomes available.
Scheduled runs prefer the stable Windows PowerShell 5.1 system executable.
If unavailable, an existing non-Store pwsh.exe on PATH is required.
#>
[CmdletBinding(DefaultParameterSetName = 'Status')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Enable')][switch]$Enabled,
    [Parameter(Mandatory, ParameterSetName = 'Disable')][switch]$Disable,
    [Parameter(ParameterSetName = 'Status')][switch]$Status,
    [ValidatePattern('\A([01][0-9]|2[0-3]):[0-5][0-9]\z')][string]$Time = '07:00',
    [Parameter(Mandatory, ParameterSetName = 'Enable')][string]$PythonPath,
    [string]$RepoRoot,
    [string]$DataDirectory
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$taskName = 'FoundryModelInventory-MorningScan'
$taskPath = '\'
$note = 'You must be signed into Windows and Azure CLI authentication must be valid. Times are local; missed runs start when the machine is available (StartWhenAvailable).'

function Get-InventoryTask {
    try {
        Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -notlike 'CmdletizationQuery_NotFound*') { throw }
    }
}

function Get-AbsoluteDirectory([string]$Value, [string]$Name) {
    if (-not [IO.Path]::IsPathRooted($Value) -or $Value -match '["\r\n]') {
        throw "$Name must be an absolute local path without quotes or newlines."
    }
    [IO.Path]::GetFullPath($Value).TrimEnd('\')
}

function Quote-TaskArgument([string]$Value) {
    if ($Value -match '["\r\n]') { throw 'Task paths must not contain quotes or newlines.' }
    '"' + ($Value -replace '(\\+)$', '$1$1') + '"'
}

function Get-ScheduledPowerShellPath {
    if ($env:SystemRoot) {
        $systemRoot = Get-AbsoluteDirectory $env:SystemRoot 'SystemRoot'
        $systemHost = Join-Path $systemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if (Test-Path -LiteralPath $systemHost -PathType Leaf) { return $systemHost }
    }
    try {
        $fallback = Get-Command pwsh.exe -CommandType Application -ErrorAction Stop
    } catch [System.Management.Automation.CommandNotFoundException] {
        throw 'Windows PowerShell is unavailable and no non-Store pwsh.exe fallback is installed on PATH.'
    }
    $path = [string]$fallback.Source
    $invalid = 'PowerShell fallback must be an existing absolute, non-WindowsApps pwsh.exe.'
    if ($path -notmatch '\A(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+\\)' -or $path -match '["\r\n]') {
        throw $invalid
    }
    $path = [IO.Path]::GetFullPath($path)
    if ([IO.Path]::GetFileName($path) -ine 'pwsh.exe' -or $path -match '(?i)\\WindowsApps\\' -or
        -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw $invalid
    }
    return $path
}

function Resolve-TaskOwnerSid([string]$Account) {
    $unresolved = 'The named task owner could not be resolved to a Windows SID; it will not be changed.'
    if ([string]::IsNullOrWhiteSpace($Account)) { throw $unresolved }
    try {
        if ($Account -match '\AS-\d-') {
            return [Security.Principal.SecurityIdentifier]::new($Account).Value
        }
        $reference = [Security.Principal.NTAccount]::new($Account)
        return $reference.Translate([Security.Principal.SecurityIdentifier]).Value
    } catch [System.ArgumentException], [Security.Principal.IdentityNotMappedException] {
        throw $unresolved
    }
}

function Assert-CurrentUserTask($Task, $Identity) {
    if ($null -eq $Task) { return }
    $currentSid = [string]$Identity.User.Value
    if ([string]::IsNullOrWhiteSpace($currentSid)) {
        throw 'The current Windows SID is unavailable; the named task will not be changed.'
    }
    # Task Scheduler can return an unqualified account name rather than a SID.
    $ownerSid = Resolve-TaskOwnerSid ([string]$Task.Principal.UserId)
    if ($ownerSid -ne $currentSid) {
        throw 'The named task belongs to another user; it will not be changed.'
    }
}

function Convert-RunTime($Value) {
    if ($null -eq $Value -or ([datetime]$Value).Year -lt 2000) { return $null }
    ([datetime]$Value).ToString('o')
}

if ($env:OS -ne 'Windows_NT') { throw 'This helper requires Windows Task Scheduler.' }
$task = Get-InventoryTask
if ($Enabled) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    Assert-CurrentUserTask $task $identity
    if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
    $RepoRoot = Get-AbsoluteDirectory $RepoRoot 'RepoRoot'
    if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) { throw 'RepoRoot does not exist.' }
    if (-not $DataDirectory) { $DataDirectory = Join-Path $RepoRoot 'data' }
    $DataDirectory = Get-AbsoluteDirectory $DataDirectory 'DataDirectory'
    $allowed = Join-Path $RepoRoot 'data'
    if ($DataDirectory -ine $allowed -and -not $DataDirectory.StartsWith($allowed + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'DataDirectory must remain under the repository data directory.'
    }
    if (-not [IO.Path]::IsPathRooted($PythonPath) -or $PythonPath -match '["\r\n]') {
        throw 'PythonPath must be an absolute executable path.'
    }
    $PythonPath = [IO.Path]::GetFullPath($PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf) -or [IO.Path]::GetExtension($PythonPath) -ine '.exe') {
        throw 'PythonPath must point to an existing Python executable.'
    }
    $runner = Join-Path $RepoRoot 'scripts\run-morning-scan.ps1'
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'The checked-in morning runner is missing.' }
    $powerShellPath = Get-ScheduledPowerShellPath
    $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {0} -PythonPath {1} -RepoRoot {2} -DataDirectory {3}' -f `
        (Quote-TaskArgument $runner), (Quote-TaskArgument $PythonPath), `
        (Quote-TaskArgument $RepoRoot), (Quote-TaskArgument $DataDirectory)
    $action = New-ScheduledTaskAction -Execute $powerShellPath -Argument $arguments -WorkingDirectory $RepoRoot
    $at = [datetime]::Today.AddHours([int]$Time.Substring(0, 2)).AddMinutes([int]$Time.Substring(3, 2))
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $note -Force -ErrorAction Stop | Out-Null
    $task = Get-InventoryTask
} elseif ($Disable -and $null -ne $task) {
    Assert-CurrentUserTask $task ([Security.Principal.WindowsIdentity]::GetCurrent())
    Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop | Out-Null
    $task = Get-InventoryTask
}

$result = [ordered]@{
    enabled = $false
    time = $Time
    task_name = $taskName
    next_run = $null
    last_run = $null
    last_result = $null
    note = $note
}
if ($null -ne $task) {
    $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
    $result.enabled = [bool]$task.Settings.Enabled
    $boundary = @($task.Triggers | Where-Object { $_.StartBoundary } | Select-Object -First 1)
    if ($boundary.Count) { $result.time = ([datetime]$boundary[0].StartBoundary).ToString('HH:mm') }
    $result.next_run = Convert-RunTime $info.NextRunTime
    $result.last_run = Convert-RunTime $info.LastRunTime
    $result.last_result = [long]$info.LastTaskResult
} else {
    $result.note = 'The morning task is not registered. ' + $note
}
$result | ConvertTo-Json -Compress
