<# Stops the UIA -> OneBot bridge started by start-bridge.ps1. #>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir      = Join-Path $ProjectRoot 'logs'
$PidFile     = Join-Path $LogDir 'uia-bridge.pid'
$HoldFile    = Join-Path $LogDir 'uia-bridge-hold'
$found       = $false

# The normal case: the command line says who it is.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'onebot_uia_bridge' } |
    ForEach-Object {
        $found = $true
        Write-Host ("stopping bridge pid={0}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

# A bridge started elevated (or by another user) has an unreadable command
# line, so the scan above cannot see it and stop used to report "no bridge".
# The starter always records the launcher pid; take it and its child with us.
if (Test-Path -LiteralPath $PidFile) {
    $raw = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $bridgePid = 0
    if ($raw -and [int]::TryParse([string]$raw.Trim(), [ref]$bridgePid) -and $bridgePid -gt 0) {
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$bridgePid" -ErrorAction SilentlyContinue)) {
            if ($child.Name -notmatch '^python') { continue }
            $found = $true
            Write-Host ("stopping bridge child pid={0}" -f $child.ProcessId)
            Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
        }
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$bridgePid" -ErrorAction SilentlyContinue
        if ($parent -and $parent.Name -match '^python') {
            $found = $true
            Write-Host ("stopping bridge pid={0}" -f $bridgePid)
            Stop-Process -Id $bridgePid -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not $found) { Write-Host 'no UIA bridge process found' }

# Tell the watcher this stop was on purpose, so it does not start it straight back up.
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
[System.IO.File]::WriteAllText($HoldFile, (Get-Date -Format 's'))
Write-Host ("watcher hold set: {0}" -f $HoldFile)
