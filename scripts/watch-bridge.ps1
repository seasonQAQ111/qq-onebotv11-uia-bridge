<#
.SYNOPSIS
  Restart the UIA -> OneBot bridge when its process is gone.

.DESCRIPTION
  The bridge drives the real QQ window through UI Automation, so when its
  process dies the bot stops answering entirely and nothing else notices. This
  watcher checks every IntervalSeconds and starts it again.

  A deliberate stop must not be undone: scripts\stop-bridge.ps1 drops a hold
  file (logs\uia-bridge-hold) and the watcher then leaves the bridge alone until
  scripts\start-bridge.ps1 clears that file again.

  QQ is never launched by this script. Without the QQ window there is nothing to
  attach to, so the bridge would exit on start-up over and over; the watcher
  simply waits until QQ is running.

  Run it hidden:
      wscript.exe scripts\watch-bridge-detached.vbs
#>
[CmdletBinding()]
param(
    [int]$IntervalSeconds = 30,
    [int]$StartupGraceSeconds = 25,
    [string]$Config
)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$LogDir    = Join-Path $Root 'logs'
if (-not $Config) { $Config = Join-Path $ScriptDir 'bridge-config.json' }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$WatchLog = Join-Path $LogDir 'uia-bridge-watch.log'
$HoldFile = Join-Path $LogDir 'uia-bridge-hold'
$Starter  = Join-Path $ScriptDir 'start-bridge.ps1'
$SelfShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Write-Line([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $WatchLog -Value $line -Encoding UTF8
}

function Get-BridgeProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'onebot_uia_bridge' })
}

function Get-BridgeMutexName([string]$ConfigPath) {
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($ConfigPath)
    $safe = [regex]::Replace($stem, '[^A-Za-z0-9._-]', '_')
    if (-not $safe) { $safe = 'default' }
    return 'onebot_uia_bridge_' + $safe
}

function Test-BridgeRunning([string]$ConfigPath) {
    # The bridge holds a named mutex while it runs. That is the one signal that
    # still works when its command line cannot be read (elevated, another user),
    # which is exactly the case where a process scan misses it and a second
    # bridge gets started on top - and then AstrBot answers every message twice.
    try {
        $m = [System.Threading.Mutex]::OpenExisting((Get-BridgeMutexName $ConfigPath))
        $m.Dispose()
        return $true
    } catch {
        return $false
    }
}


function Test-QqRunning {
    @(Get-Process -Name 'QQ' -ErrorAction SilentlyContinue).Count -gt 0
}

Write-Line ("watcher up: interval={0}s hold={1}" -f $IntervalSeconds, $HoldFile)
while ($true) {
    if ([System.IO.File]::Exists($HoldFile)) {
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }
    if ((Get-BridgeProcesses).Count -eq 0 -and -not (Test-BridgeRunning $Config)) {
        if (-not (Test-QqRunning)) {
            Write-Line 'QQ is not running - leaving the bridge alone'
            Start-Sleep -Seconds $IntervalSeconds
            continue
        }
        Write-Line 'bridge is not running - starting it'
        Start-Process -FilePath $SelfShell `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Starter) `
            -WindowStyle Hidden
        Start-Sleep -Seconds $StartupGraceSeconds
        $after = Get-BridgeProcesses
        if ($after.Count -eq 0) { Write-Line 'bridge did not come up - will retry next round' }
        else { Write-Line ('bridge back up: pid={0}' -f $after[0].ProcessId) }
    }
    Start-Sleep -Seconds $IntervalSeconds
}