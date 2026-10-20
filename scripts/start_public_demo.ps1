param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$Domain = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'public_process.ps1')
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimePath = Join-Path $workspacePath '.runtime'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$ngrokPath = (Get-Command ngrok -ErrorAction Stop).Source
Import-ModelEnvironment
$Domain = Resolve-PublicDemoDomain $Domain (Join-Path $runtimePath 'public-status.json')

function Invoke-ServiceProbe([string]$BaseUrl) {
    $arguments = @('-m', 'scripts.public_service_probe')
    if ($BaseUrl) { $arguments += @('--base-url', $BaseUrl) }
    $raw = & $pythonPath @arguments
    try { return ($raw | ConvertFrom-Json) }
    catch { throw 'Service probe did not return a valid status. Check Python dependencies.' }
}
function Get-ListenerIdentity([int]$ListenPort) {
    $listener = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) { return (Get-ProcessIdentity $listener.OwningProcess) }
    return $null
}
function Get-MatchingTunnel {
    try {
        $state = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2
        return ($state.tunnels | Where-Object {
            $_.public_url -eq "https://$Domain" -and
            $_.config.addr -in @("http://localhost:$Port", "http://127.0.0.1:$Port", "localhost:$Port", "127.0.0.1:$Port", "$Port")
        } | Select-Object -First 1)
    } catch { return $null }
}

Push-Location $workspacePath
try {
    if (-not (Invoke-ServiceProbe '').ready) { throw 'Public authentication is not configured safely. Refusing to open a tunnel.' }
    & $ngrokPath config check | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'ngrok configuration check failed.' }
    $appIdentity = Get-ListenerIdentity $Port
    if ($appIdentity) {
        if ($appIdentity.ExecutablePath -ne $pythonPath -or $appIdentity.CommandLine -notmatch 'uvicorn\s+tunnel_qa\.main:app\s' -or $appIdentity.CommandLine -notmatch "--port\s+$Port(?:\s|$)") {
            throw "Port $Port belongs to an unverified process. It has not been stopped or reused."
        }
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $appProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'tunnel_qa.main:app', '--host', '127.0.0.1', '--port', "$Port") -WorkingDirectory $workspacePath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath "uvicorn-$stamp.out.log") -RedirectStandardError (Join-Path $runtimePath "uvicorn-$stamp.error.log")
        Save-ProcessIdentity (Join-Path $runtimePath 'uvicorn.process.json') $appProcess.Id
        for ($attempt = 0; $attempt -lt 180; $attempt++) {
            if ($appProcess.HasExited) { throw 'Application exited during startup. Inspect the newest uvicorn log in .runtime.' }
            $appIdentity = Get-ListenerIdentity $Port
            if ($appIdentity) { break }
            Start-Sleep -Seconds 1
        }
        if (-not $appIdentity -or $appIdentity.ProcessId -ne $appProcess.Id) { throw 'Application listener could not be verified.' }
    }
    $localCheck = Invoke-ServiceProbe "http://127.0.0.1:$Port"
    if (-not $localCheck.ready) { throw "Local authentication/health check failed: $($localCheck.error). Tunnel startup refused." }
    Save-ProcessIdentity (Join-Path $runtimePath 'uvicorn.process.json') $appIdentity.ProcessId

    $tunnel = Get-MatchingTunnel
    $tunnelIdentity = Get-ListenerIdentity 4040
    if ($tunnel -and $tunnelIdentity -and [IO.Path]::GetFileName($tunnelIdentity.ExecutablePath) -eq 'ngrok.exe' -and $tunnelIdentity.CommandLine.Contains($Domain)) {
        Save-ProcessIdentity (Join-Path $runtimePath 'ngrok.process.json') $tunnelIdentity.ProcessId
    } else {
        if ($tunnelIdentity) { throw 'An unrelated or mismatched tunnel uses port 4040. It has not been stopped. Check its domain/upstream first.' }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $ngrokProcess = Start-Process -FilePath $ngrokPath -ArgumentList @('http', "--url=$Domain", "$Port") -WorkingDirectory $workspacePath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath "ngrok-$stamp.out.log") -RedirectStandardError (Join-Path $runtimePath "ngrok-$stamp.error.log")
        Save-ProcessIdentity (Join-Path $runtimePath 'ngrok.process.json') $ngrokProcess.Id
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if ($ngrokProcess.HasExited) { throw 'ngrok exited. Inspect the newest ngrok log in .runtime.' }
            $tunnel = Get-MatchingTunnel
            if ($tunnel) { break }
            Start-Sleep -Seconds 1
        }
        if (-not $tunnel) { throw 'Tunnel registration timed out. Local application is still available; rerun after checking network connectivity.' }
        $tunnelIdentity = Get-ProcessIdentity $ngrokProcess.Id
    }
    $publicCheck = Invoke-ServiceProbe "https://$Domain"
    $report = [pscustomobject]@{
        PublicUrl = "https://$Domain"
        AppPid = $appIdentity.ProcessId
        TunnelPid = $tunnelIdentity.ProcessId
        LocalReady = $localCheck.ready
        TunnelRegistered = $true
        PublicReady = $publicCheck.ready
        PublicCheck = $publicCheck
        CheckedAt = (Get-Date).ToUniversalTime().ToString('o')
    }
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimePath 'public-status.json') -Encoding utf8
    $report | ConvertTo-Json -Depth 4
    if (-not $publicCheck.ready) { throw 'Tunnel registered, but public access is NOT verified. Check network/ngrok availability and rerun. Services have been preserved.' }
} finally { Pop-Location }
