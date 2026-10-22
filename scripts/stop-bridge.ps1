<# Stops the UIA -> OneBot bridge started by start-bridge.ps1. #>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir

$found = $false
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'onebot_uia_bridge' } |
    ForEach-Object {
        $found = $true
        Write-Host ("stopping bridge pid={0}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
if (-not $found) { Write-Host 'no UIA bridge process found' }

# Tell the watcher this stop was on purpose, so it does not start it straight back up.
$LogDir   = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$HoldFile = Join-Path $LogDir 'uia-bridge-hold'
[System.IO.File]::WriteAllText($HoldFile, (Get-Date -Format 's'))
Write-Host ("watcher hold set: {0}" -f $HoldFile)