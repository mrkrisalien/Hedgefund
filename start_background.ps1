$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $here "run_server.bat"
$cmd = "cmd.exe"
$arg = "/c `"$bat`""
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine = "$cmd $arg"
  CurrentDirectory = $here
} | Out-Null
Write-Host "Started autonomous engine via run_server.bat (detached)."
