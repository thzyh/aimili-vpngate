[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

Assert-PrototypePortsFree
$before = Get-HostSafetySnapshot
Write-Output 'preflight=passed'

if ($CheckOnly) {
    exit 0
}

Ensure-DockerEngine
New-Item -ItemType Directory -Path $script:PrototypeStateDirectory -Force | Out-Null
$before | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $script:PrototypeBaselineFile -Encoding UTF8

try {
    if ($SkipBuild) {
        Invoke-PrototypeCompose -Arguments @('up', '-d')
    }
    else {
        Invoke-PrototypeCompose -Arguments @('up', '-d', '--build')
    }
    $after = Get-HostSafetySnapshot
    Assert-HostSafetyUnchanged -Before $before -After $after
}
catch {
    $afterFailure = Get-HostSafetySnapshot
    Assert-HostSafetyUnchanged -Before $before -After $afterFailure
    throw
}

Write-Output 'prototype=started'
