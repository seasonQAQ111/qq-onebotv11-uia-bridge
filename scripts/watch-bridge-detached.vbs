' Start the UIA bridge watcher hidden and outside the caller's process tree.
' Explorer owns the child, so the watcher keeps running after whatever started
' it has exited. Both the script and the shell are found relative to this file,
' so the folder can live anywhere.
Option Explicit

Dim fso, shell, q, scriptDir, root, exe, script, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
q = Chr(34)

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(scriptDir)
exe = fso.BuildPath(shell.ExpandEnvironmentStrings("%SystemRoot%"), "System32\WindowsPowerShell\v1.0\powershell.exe")
script = fso.BuildPath(scriptDir, "watch-bridge.ps1")

cmd = q & exe & q & " -NoProfile -ExecutionPolicy Bypass -File " & q & script & q
shell.Run cmd, 0, False