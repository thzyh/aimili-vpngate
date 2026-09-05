[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$python = @'
import json
from pathlib import Path
value = json.loads(Path('/var/lib/aimilivpn/ui_auth.json').read_text(encoding='utf-8'))
print(json.dumps({
    'url': 'http://127.0.0.1:18787/' + value['secret_path'].strip('/') + '/',
    'username': value['username'],
    'password': value['password'],
}))
'@
$raw = & docker compose --project-name $script:PrototypeProjectName -f $script:PrototypeComposeFile exec -T aimilivpn-single python3 -c $python
if ($LASTEXITCODE -ne 0) {
    throw 'unable to read local prototype access details'
}
$access = $raw | ConvertFrom-Json
Write-Output ("URL: {0}" -f $access.url)
Write-Output ("Username: {0}" -f $access.username)
Write-Output ("Password: {0}" -f $access.password)
