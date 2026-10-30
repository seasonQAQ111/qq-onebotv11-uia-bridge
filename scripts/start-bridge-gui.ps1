<#
.SYNOPSIS
  Open the QQ bridge desktop console.
.DESCRIPTION
  Starts scripts\qq_bridge_gui.py with the repository virtualenv. pythonw.exe
  is preferred so the interactive console does not keep an extra shell window
  open behind it.
#>
[CmdletBinding()]
param(
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir

function Resolve-Python([string]$Explicit) {
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return $Explicit }
        throw "python not found: $Explicit"
    }
    foreach ($candidate in @(
        (Join-Path $Root '.venv\Scripts\python.exe'),
        (Join-Path $Root 'venv\Scripts\python.exe')
    )) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw 'no python interpreter found. Pass -Python <path> or create .venv in the repo root.'
}

$pythonPath = Resolve-Python $Python
$gui = Join-Path $ScriptDir 'qq_bridge_gui.py'
$windowed = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
$executable = if (Test-Path -LiteralPath $windowed) { $windowed } else { $pythonPath }

Start-Process -FilePath $executable -ArgumentList @($gui) -WorkingDirectory $Root
Write-Host 'QQ bridge console started.'
