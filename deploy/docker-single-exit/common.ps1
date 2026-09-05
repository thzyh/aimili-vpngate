Set-StrictMode -Version Latest

$script:PrototypeComposeFile = Join-Path $PSScriptRoot 'compose.yaml'
$script:PrototypeProjectName = 'aimili-single-exit'
$script:PrototypeStateDirectory = Join-Path $env:LOCALAPPDATA 'AimiliGateway\docker-single-exit'
$script:PrototypeBaselineFile = Join-Path $script:PrototypeStateDirectory 'host-baseline.json'

function Get-DefaultRouteDigest {
    $routes = @(Get-NetRoute -ErrorAction Stop | Where-Object {
        $_.DestinationPrefix -in @('0.0.0.0/0', '::/0')
    } | Sort-Object AddressFamily, InterfaceIndex, NextHop, RouteMetric | ForEach-Object {
        [pscustomobject]@{
            AddressFamily = [string]$_.AddressFamily
            InterfaceIndex = [int]$_.InterfaceIndex
            NextHop = [string]$_.NextHop
            RouteMetric = [int]$_.RouteMetric
        }
    })
    $payload = $routes | ConvertTo-Json -Compress
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '')
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-CurrentProxyHealth {
    param(
        [int]$ProxyEnable,
        [string]$ProxyServer
    )
    if ($ProxyEnable -ne 1 -or $ProxyServer -ne '127.0.0.1:10808') {
        return 'not-applicable'
    }
    try {
        $response = Invoke-WebRequest -Uri 'https://www.gstatic.com/generate_204' -Proxy 'http://127.0.0.1:10808' -TimeoutSec 15 -UseBasicParsing
        return [string]$response.StatusCode
    }
    catch {
        return 'unhealthy'
    }
}

function Get-HostSafetySnapshot {
    $settings = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
    $proxyEnable = [int]$settings.ProxyEnable
    $proxyServer = [string]$settings.ProxyServer
    [pscustomobject]@{
        V2rayNPids = @(
            Get-CimInstance Win32_Process -Filter "Name = 'v2rayN.exe'" -ErrorAction Stop |
                Sort-Object ProcessId |
                ForEach-Object { [int]$_.ProcessId }
        )
        ProxyEnable = $proxyEnable
        ProxyServer = $proxyServer
        DefaultRouteDigest = Get-DefaultRouteDigest
        CurrentProxyHealth = Get-CurrentProxyHealth -ProxyEnable $proxyEnable -ProxyServer $proxyServer
    }
}

function Assert-HostSafetyUnchanged {
    param(
        [Parameter(Mandatory)]$Before,
        [Parameter(Mandatory)]$After
    )
    if ((@($Before.V2rayNPids) -join ',') -ne (@($After.V2rayNPids) -join ',')) {
        throw 'v2rayN process identity changed during prototype operation'
    }
    if ([int]$Before.ProxyEnable -ne [int]$After.ProxyEnable -or [string]$Before.ProxyServer -ne [string]$After.ProxyServer) {
        throw 'Windows system proxy changed during prototype operation'
    }
    if ([string]$Before.DefaultRouteDigest -ne [string]$After.DefaultRouteDigest) {
        throw 'Windows default route changed during prototype operation'
    }
    if ([string]$Before.CurrentProxyHealth -eq '204' -and [string]$After.CurrentProxyHealth -ne '204') {
        throw 'the current v2rayN proxy health check no longer passes'
    }
}

function Assert-PrototypePortsFree {
    foreach ($port in @(17928, 18787)) {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        if ($listeners.Count -ne 0 -and -not (Test-PrototypeOwnsPort -PublishedPort $port)) {
            throw "local prototype port $port is already in use"
        }
    }
}

function Test-PrototypeOwnsPort {
    param([Parameter(Mandatory)][int]$PublishedPort)
    if (-not (Test-DockerEngineAvailable)) {
        return $false
    }
    $containerId = (& docker compose --project-name $script:PrototypeProjectName -f $script:PrototypeComposeFile ps -q aimilivpn-single 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $containerId) {
        return $false
    }
    $targetPort = if ($PublishedPort -eq 17928) { 7928 } elseif ($PublishedPort -eq 18787) { 8787 } else { return $false }
    $bindings = @(& docker port $containerId "$targetPort/tcp" 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    return @($bindings | Where-Object { $_.Trim() -eq "127.0.0.1:$PublishedPort" }).Count -eq 1
}

function Invoke-PrototypeCompose {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & docker compose --project-name $script:PrototypeProjectName -f $script:PrototypeComposeFile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

function Test-DockerEngineAvailable {
    & cmd.exe /d /s /c 'docker info --format "{{.ServerVersion}}" >nul 2>nul'
    return $LASTEXITCODE -eq 0
}

function Get-PrototypeRuntimeState {
    $containerId = (& docker compose --project-name $script:PrototypeProjectName -f $script:PrototypeComposeFile ps -q aimilivpn-single).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $containerId) {
        throw 'single-exit container is not running'
    }
    $health = (& docker inspect --format '{{.State.Health.Status}}' $containerId).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'unable to inspect single-exit container health'
    }
    $probe = "import json,pathlib,subprocess; result=subprocess.run(['pgrep','-xc','openvpn'],capture_output=True,text=True); print(json.dumps({'tun0':pathlib.Path('/sys/class/net/tun0').is_dir(),'openvpn':int((result.stdout.strip() or '0'))}))"
    $raw = & docker compose --project-name $script:PrototypeProjectName -f $script:PrototypeComposeFile exec -T aimilivpn-single python3 -c $probe
    if ($LASTEXITCODE -ne 0) {
        throw 'unable to inspect single-exit runtime state'
    }
    $runtime = $raw | ConvertFrom-Json
    [pscustomobject]@{
        ContainerRunning = $true
        Health = $health
        Tun0 = [bool]$runtime.tun0
        OpenVPNProcesses = [int]$runtime.openvpn
    }
}

function Ensure-DockerEngine {
    if (Test-DockerEngineAvailable) {
        return
    }
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $desktop -PathType Leaf)) {
        throw 'Docker Desktop is not installed at the expected location'
    }
    Start-Process -FilePath $desktop -WindowStyle Hidden
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Seconds 3
        if (Test-DockerEngineAvailable) {
            return
        }
    } while ((Get-Date) -lt $deadline)
    throw 'Docker Engine did not become ready within three minutes'
}
