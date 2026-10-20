# PID alone is never sufficient to stop a process.
function Resolve-PublicDemoDomain([string]$Domain, [string]$StatusPath) {
    if ([string]::IsNullOrWhiteSpace($Domain)) { $Domain = $env:TUNNEL_QA_PUBLIC_DOMAIN }
    if ([string]::IsNullOrWhiteSpace($Domain) -and (Test-Path -LiteralPath $StatusPath)) {
        try {
            $saved = Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json
            $url = [uri]$saved.PublicUrl
            if ($url.IsAbsoluteUri -and $url.Scheme -eq 'https' -and $url.IsDefaultPort -and
                -not $url.UserInfo -and -not $url.Query -and -not $url.Fragment -and $url.AbsolutePath -eq '/') {
                $Domain = $url.DnsSafeHost
            }
        } catch { }
    }
    if ([string]::IsNullOrWhiteSpace($Domain) -or $Domain -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]$') {
        throw 'Specify -Domain <your-ngrok-domain> or TUNNEL_QA_PUBLIC_DOMAIN. No valid saved domain was found.'
    }
    return $Domain
}

function Get-ProcessIdentity([int]$ProcessId) {
    $item = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $item -or -not $item.ExecutablePath -or -not $item.CommandLine) { return $null }
    return [pscustomobject]@{
        ProcessId = [int]$item.ProcessId
        ExecutablePath = $item.ExecutablePath
        CommandLine = $item.CommandLine
        CreatedTicks = $item.CreationDate.ToUniversalTime().Ticks.ToString()
    }
}

function Test-ProcessIdentity($Recorded, $Current) {
    return ($null -ne $Recorded -and $null -ne $Current -and
        $Recorded.ProcessId -eq $Current.ProcessId -and
        $Recorded.ExecutablePath -eq $Current.ExecutablePath -and
        $Recorded.CommandLine -ceq $Current.CommandLine -and
        $Recorded.CreatedTicks -ceq $Current.CreatedTicks)
}

function Save-ProcessIdentity([string]$Path, [int]$ProcessId) {
    $identity = Get-ProcessIdentity $ProcessId
    if (-not $identity) { throw 'Unable to verify launched process identity.' }
    $identity | ConvertTo-Json | Set-Content -LiteralPath $Path -Encoding utf8
}

function Import-ModelEnvironment {
    # Respect explicit process values. Never echo or persist secret values.
    foreach ($name in @('DEEPSEEK_API_KEY', 'TUNNEL_QA_DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'TUNNEL_QA_DEEPSEEK_BASE_URL', 'DEEPSEEK_MODEL', 'TUNNEL_QA_DEEPSEEK_MODEL', 'TUNNEL_QA_SECRETS_ENCRYPTION_KEY')) {
        if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
            $value = [Environment]::GetEnvironmentVariable($name, 'User')
            if (-not $value) { $value = [Environment]::GetEnvironmentVariable($name, 'Machine') }
            if ($value) { [Environment]::SetEnvironmentVariable($name, $value, 'Process') }
        }
    }
}
