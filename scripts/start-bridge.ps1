<#
.SYNOPSIS
  Starts the UIA -> OneBot v11 bridge (drives the real QQ client, no injection).

.DESCRIPTION
  The bridge drives the QQ desktop client with Windows UI Automation and
  presents it to AstrBot as a normal OneBot reverse-WS client, so every plugin,
  persona and memory file keeps working unchanged.

  Conversations are reached through QQ's session list and every send verifies
  the chat header before typing, so multi-session switching cannot drop a reply
  into the wrong chat.

  Run one OneBot client per AstrBot instance only. If NapCat / Lagrange /
  SnowLuma is already connected to the same AstrBot, stop it first: two clients
  reply twice to every message.

    powershell -File scripts\start-bridge.ps1
    powershell -File scripts\start-bridge.ps1 -DryRun      # read only, never send
    powershell -File scripts\start-bridge.ps1 -SelfCheck   # report config vs QQ, exit
    powershell -File scripts\start-bridge.ps1 -Config D:\path\bridge-config.json
    powershell -File scripts\start-bridge.ps1 -Python D:\venv\Scripts\python.exe
#>
[CmdletBinding()]
param(
    [string]$Config,
    [string]$Python,
    [switch]$DryRun,
    [switch]$SelfCheck
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir

function Resolve-Python([string]$Explicit) {
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return $Explicit }
        throw "python not found: $Explicit"
    }
    foreach ($c in @((Join-Path $Root '.venv\Scripts\python.exe'),
                     (Join-Path $Root 'venv\Scripts\python.exe'))) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'no python interpreter found. Pass -Python <path> or create .venv in the repo root.'
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


$Py     = Resolve-Python $Python
$Bridge = Join-Path $ScriptDir 'onebot_uia_bridge.py'

if (-not $Config) { $Config = Join-Path $ScriptDir 'bridge-config.json' }
if (-not (Test-Path -LiteralPath $Config)) {
    Write-Error ("config not found: {0}`ncopy scripts\bridge-config.example.json to scripts\bridge-config.json and edit it." -f $Config)
    exit 2
}

$LogDir   = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$OutLog   = Join-Path $LogDir 'uia-bridge.log'
$ErrLog   = Join-Path $LogDir 'uia-bridge.err.log'
$PidFile  = Join-Path $LogDir 'uia-bridge.pid'
$HoldFile = Join-Path $LogDir 'uia-bridge-hold'

# Starting on purpose cancels a previous stop, so the watcher looks after it again.
if ([System.IO.File]::Exists($HoldFile)) {
    [System.IO.File]::Delete($HoldFile)
    Write-Host 'cleared the watcher hold file'
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

if ($SelfCheck) {
    & $Py $Bridge --config $Config --selfcheck
    exit $LASTEXITCODE
}

Get-BridgeProcesses | ForEach-Object {
    Write-Host ("stopping previous bridge pid={0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 600

# A bridge that was started elevated (or by another user) has an unreadable
# command line, so the scan above cannot find it - but its mutex is still there.
# Starting a second bridge anyway would make AstrBot answer every message twice.
if (Test-BridgeRunning $Config) {
    Write-Error ("a bridge for {0} is still running and could not be stopped from here." -f $Config)
    Write-Host 'Its command line is unreadable, which usually means it was started elevated or by another user.'
    Write-Host 'Stop it in that window, then start this one again.'
    exit 3
}

$argList = @($Bridge, '--config', $Config)
if ($DryRun) { $argList += '--dry-run' }

$p = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $Root `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
$p.Id | Set-Content -LiteralPath $PidFile -Encoding ASCII

Write-Host ("UIA bridge started: pid={0}" -f $p.Id)
Write-Host ("python : {0}" -f $Py)
Write-Host ("config : {0}" -f $Config)
Write-Host ("log    : {0}" -f $OutLog)
if ($DryRun) { Write-Host 'mode   : DRY-RUN (reads only, never types into QQ)' }
Write-Host ''
Write-Host 'Run scripts\start-bridge.ps1 -SelfCheck to compare the config against live QQ.'
Write-Host 'Run scripts\watch-bridge-detached.vbs to keep it alive in the background.'