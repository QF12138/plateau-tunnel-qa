$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/public_process.ps1')
$sample = [pscustomobject]@{ProcessId=123;ExecutablePath='C:\test\python.exe';CommandLine='python -m uvicorn test:app';CreatedTicks='1234567'}
$same = $sample | ConvertTo-Json | ConvertFrom-Json
if (-not (Test-ProcessIdentity $sample $same)) { throw 'Matching identity rejected' }
foreach ($field in @('ProcessId', 'ExecutablePath', 'CommandLine', 'CreatedTicks')) {
    $changed = $sample | ConvertTo-Json | ConvertFrom-Json
    if ($field -eq 'ProcessId') { $changed.$field = 124 } else { $changed.$field = 'changed' }
    if (Test-ProcessIdentity $sample $changed) { throw "Mismatched $field accepted" }
}
if (Test-ProcessIdentity $sample $null) { throw 'Missing process accepted' }
$previousDomain = $env:TUNNEL_QA_PUBLIC_DOMAIN
try {
    $env:TUNNEL_QA_PUBLIC_DOMAIN = 'environment-example.ngrok-free.dev'
    $absentStatus = Join-Path $PSScriptRoot 'no-local-public-status.json'
    if ((Resolve-PublicDemoDomain 'explicit-example.ngrok-free.dev' $absentStatus) -ne 'explicit-example.ngrok-free.dev') { throw 'Explicit domain did not win' }
    if ((Resolve-PublicDemoDomain '' $absentStatus) -ne $env:TUNNEL_QA_PUBLIC_DOMAIN) { throw 'Environment domain was not used' }
    $env:TUNNEL_QA_PUBLIC_DOMAIN = ''
    $rejected = $false
    try { Resolve-PublicDemoDomain '' $absentStatus | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Missing domain accepted' }
    foreach ($invalidDomain in @('https://example.test', 'bad domain', 'example.test/path', 'example.test:8765')) {
        $rejected = $false
        try { Resolve-PublicDemoDomain $invalidDomain $absentStatus | Out-Null } catch { $rejected = $true }
        if (-not $rejected) { throw 'Unsafe domain accepted' }
    }
} finally { $env:TUNNEL_QA_PUBLIC_DOMAIN = $previousDomain }
foreach ($file in @('start_public_demo.ps1', 'stop_public_demo.ps1', 'public_process.ps1')) {
    $parseErrors = $null
    $parseTokens = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot "../scripts/$file"), [ref]$parseTokens, [ref]$parseErrors)
    if ($parseErrors) { throw "Parse failed: $file" }
}
Write-Output 'Process identity and launcher syntax checks passed (no processes stopped).'
