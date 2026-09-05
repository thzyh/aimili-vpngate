[CmdletBinding()]
param(
    [switch]$PurgeData,
    [switch]$Plan
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if ($Plan) {
    [pscustomobject]@{
        ProjectName = $script:PrototypeProjectName
        PurgeData = [bool]$PurgeData
    } | ConvertTo-Json -Compress
    exit 0
}

if ($PurgeData) {
    Invoke-PrototypeCompose -Arguments @('down', '--volumes', '--remove-orphans')
    if (Test-Path -LiteralPath $script:PrototypeBaselineFile -PathType Leaf) {
        Remove-Item -LiteralPath $script:PrototypeBaselineFile -Force
    }
    Write-Output 'prototype=stopped data=removed'
}
else {
    Invoke-PrototypeCompose -Arguments @('down', '--remove-orphans')
    Write-Output 'prototype=stopped data=preserved'
}
