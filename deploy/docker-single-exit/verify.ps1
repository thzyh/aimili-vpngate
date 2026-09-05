[CmdletBinding()]
param(
    [ValidateRange(60, 1800)]
    [int]$TimeoutSeconds = 900,
    [switch]$RuntimeProbeOnly
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if ($RuntimeProbeOnly) {
    Get-PrototypeRuntimeState | ConvertTo-Json -Compress
    exit 0
}

if (-not (Test-Path -LiteralPath $script:PrototypeBaselineFile -PathType Leaf)) {
    throw 'host safety baseline is missing; run start.ps1 first'
}
$before = Get-Content -LiteralPath $script:PrototypeBaselineFile -Raw | ConvertFrom-Json
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$ready = $false

do {
    $runtime = Get-PrototypeRuntimeState
    $runtimeReady = $runtime.Health -eq 'healthy' -and $runtime.Tun0 -and $runtime.OpenVPNProcesses -eq 1

    if ($runtimeReady) {
        $proxyExit = (& curl.exe --silent --show-error --fail --max-time 20 --proxy 'http://127.0.0.1:17928' 'https://api.ipify.org').Trim()
        $proxyAddress = $null
        $proxyValid = [Net.IPAddress]::TryParse($proxyExit, [ref]$proxyAddress)

        $directExit = (& docker compose --project-name $script:PrototypeProjectName -f $script:PrototypeComposeFile exec -T aimilivpn-single curl --silent --show-error --fail --max-time 20 --interface eth0 'https://api.ipify.org').Trim()
        $directAddress = $null
        $directValid = [Net.IPAddress]::TryParse($directExit, [ref]$directAddress)

        if ($proxyValid -and $directValid -and $proxyExit -ne $directExit) {
            $ready = $true
            break
        }
    }
    Start-Sleep -Seconds 5
} while ((Get-Date) -lt $deadline)

if (-not $ready) {
    throw 'single-exit runtime did not establish a distinct verified VPN egress before timeout'
}

$after = Get-HostSafetySnapshot
Assert-HostSafetyUnchanged -Before $before -After $after

[pscustomobject]@{
    Status = 'ready'
    OpenVPNProcesses = 1
    Tun0 = $true
    ProxyEgressValid = $true
    ProxyEgressDiffersFromContainerDirect = $true
    HostSafety = 'unchanged'
} | ConvertTo-Json -Compress
