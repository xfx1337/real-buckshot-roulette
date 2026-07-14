<#
    Auto-detect network parameters and write to config.json (Windows / PowerShell).
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Flash
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir
$Config    = Join-Path $RootDir 'config.json'

if (-not (Test-Path $Config)) {
    Write-Error "Could not find $Config. Copy config.example.json to config.json."
    exit 1
}

# -- SSID --
$Ssid = $null
$netshShow = netsh wlan show interfaces 2>$null
foreach ($line in $netshShow) {
    if ($line -match '^\s*SSID\s*:\s*(.+?)\s*$' -and $line -notmatch 'BSSID') {
        $Ssid = $Matches[1]
        break
    }
}

# -- Password --
$Password = $null
if ($Ssid) {
    $profile = netsh wlan show profile name="$Ssid" key=clear 2>$null
    foreach ($line in $profile) {
        # Using Unicode escape for "Содержимое ключа" to avoid encoding issues in script parsing
        if ($line -match '^\s*Key Content\s*:\s*(.+?)\s*$' -or
            $line -match '^\s*(\u0421\u043E\u0434\u0435\u0440\u0436\u0438\u043C\u043E\u0435\u0020\u043A\u043B\u044E\u0447\u0430)\s*:\s*(.+?)\s*$') {
            $Password = $Matches[1]
            if ($Matches.Count -gt 2) {
                $Password = $Matches[2]
            }
            break
        }
    }
}

# -- LAN-IP --
$Ip = $null
try {
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
             Sort-Object RouteMetric | Select-Object -First 1
    if ($route) {
        $Ip = (Get-NetIPAddress -InterfaceIndex $route.ifIndex -AddressFamily IPv4 `
                 -ErrorAction Stop |
               Where-Object { $_.IPAddress -notlike '169.254.*' } |
               Select-Object -First 1).IPAddress
    }
} catch {
    # Fallback
    $Ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
           Select-Object -First 1).IPAddress
}

# -- Port --
$Port = 8000
$portMatch = Select-String -Path $Config -Pattern '"port"\s*:\s*(\d+)' | Select-Object -First 1
if ($portMatch) { $Port = [int]$portMatch.Matches[0].Groups[1].Value }

$ServerUrl = $null
if ($Ip) { $ServerUrl = "http://${Ip}:${Port}" }

# -- Report --
Write-Host "> OS:            Windows"
Write-Host "> SSID:          $(if ($Ssid) { $Ssid } else { '<not determined>' })"
Write-Host "> Password:      $(if ($Password) { '<found>' } else { '<not found - fill manually>' })"
Write-Host "> Host IP:       $(if ($Ip) { $Ip } else { '<not determined>' })"
Write-Host "> server_base_url: $(if ($ServerUrl) { $ServerUrl } else { '<not built>' })"
Write-Host ""

if ($DryRun) {
    Write-Host "Info: -DryRun: config.json not modified."
    exit 0
}

# -- Replace keys in config.json --
$text = Get-Content -Path $Config -Raw -Encoding UTF8

function Set-Key {
    param([string]$Key, [string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return }
    $jsonVal = $Value -replace '\\', '\\' -replace '"', '\"'
    $pattern = '("' + [regex]::Escape($Key) + '"\s*:\s*")[^"]*(")'
    if ($script:text -match $pattern) {
        $repl = '${1}' + ($jsonVal -replace '\$', '$$$$') + '${2}'
        $script:text = [regex]::Replace($script:text, $pattern, $repl)
    } else {
        Write-Warning "Key `"$Key`" not found in config.json - skipped."
    }
}

Set-Key 'wifi_ssid'       $Ssid
Set-Key 'wifi_password'   $Password
Set-Key 'server_base_url' $ServerUrl

[System.IO.File]::WriteAllText($Config, $text, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "config.json updated."
if (-not $Ssid)      { Write-Warning "SSID not determined - check Wi-Fi and fill manually." }
if (-not $Password)  { Write-Warning "Password not found - fill esp.wifi_password manually." }
if (-not $Ip)        { Write-Warning "IP not determined - fill esp.server_base_url manually." }

if ($Flash) {
    Write-Host ""
    Write-Host "> Flashing board..."
    bash (Join-Path $RootDir 'esp/flash.sh')
}
