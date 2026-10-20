$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'public_process.ps1')
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimePath = Join-Path $workspacePath '.runtime'
foreach ($serviceName in @('ngrok', 'uvicorn')) {
    $recordPath = Join-Path $runtimePath "$serviceName.process.json"
    if (-not (Test-Path -LiteralPath $recordPath)) {
        Write-Warning "$serviceName has no verified process record; skipped. Run the updated start script first."
        continue
    }
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $current = Get-ProcessIdentity $record.ProcessId
    if (-not $current) { Write-Output "$serviceName is already stopped."; continue }
    if (-not (Test-ProcessIdentity $record $current)) {
        Write-Warning "$serviceName process identity changed; skipped to protect unrelated processes."
        continue
    }
    Stop-Process -Id $record.ProcessId
    Write-Output "Stopped verified $serviceName process $($record.ProcessId)."
}
