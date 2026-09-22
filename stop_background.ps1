$ErrorActionPreference = "SilentlyContinue"
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match "main.py" -or $_.CommandLine -match "uvicorn main:app"
} | ForEach-Object {
  Write-Host "Stopping PID $($_.ProcessId)"
  Stop-Process -Id $_.ProcessId -Force
}
$conn = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
if ($conn) {
  $conn | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
Write-Host "Stopped processes bound to main.py / port 8000."
