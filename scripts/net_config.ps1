<#
    Автосбор сетевых параметров в config.json (Windows / PowerShell).

    Запусти скрипт — он сам определит:
      • SSID текущей Wi-Fi сети            -> esp.wifi_ssid
      • пароль этой сети (netsh profile)   -> esp.wifi_password
      • LAN-IP этого хоста + порт из config.json -> esp.server_base_url
    и впишет их в корневой config.json (комментарии JSONC сохраняются —
    правятся только три строки-значения).

    Запуск (из любого места):
      ./scripts/net_config.ps1              # записать в config.json
      ./scripts/net_config.ps1 -DryRun      # только показать, что нашлось
      ./scripts/net_config.ps1 -Flash       # после записи перепрошить (esp/flash.sh через bash)

    Если PowerShell блокирует запуск скриптов:
      powershell -ExecutionPolicy Bypass -File .\scripts\net_config.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Flash
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir   # scripts/ лежит на уровень ниже корня
$Config    = Join-Path $RootDir 'config.json'

if (-not (Test-Path $Config)) {
    Write-Error "Не найден $Config. Скопируй config.example.json в config.json."
    exit 1
}

# ── SSID текущей сети ──────────────────────────────────────────────
$Ssid = $null
$netshShow = netsh wlan show interfaces 2>$null
foreach ($line in $netshShow) {
    if ($line -match '^\s*SSID\s*:\s*(.+?)\s*$' -and $line -notmatch 'BSSID') {
        $Ssid = $Matches[1]
        break
    }
}

# ── Пароль из сохранённого профиля Wi-Fi ───────────────────────────
$Password = $null
if ($Ssid) {
    $profile = netsh wlan show profile name="$Ssid" key=clear 2>$null
    foreach ($line in $profile) {
        if ($line -match '^\s*Key Content\s*:\s*(.+?)\s*$' -or
            $line -match '^\s*Содержимое ключа\s*:\s*(.+?)\s*$') {
            $Password = $Matches[1]
            break
        }
    }
}

# ── LAN-IP хоста (IPv4 интерфейса с дефолтным маршрутом) ────────────
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
    # Фолбэк: первый «настоящий» IPv4
    $Ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
           Select-Object -First 1).IPAddress
}

# ── Порт сервера из config.json ────────────────────────────────────
$Port = 8000
$portMatch = Select-String -Path $Config -Pattern '"port"\s*:\s*(\d+)' | Select-Object -First 1
if ($portMatch) { $Port = [int]$portMatch.Matches[0].Groups[1].Value }

$ServerUrl = $null
if ($Ip) { $ServerUrl = "http://${Ip}:${Port}" }

# ── Отчёт ──────────────────────────────────────────────────────────
Write-Host "> ОС:            Windows"
Write-Host "> SSID:          $(if ($Ssid) { $Ssid } else { '<не определён>' })"
Write-Host "> Пароль:        $(if ($Password) { '<найден>' } else { '<не найден — впиши вручную>' })"
Write-Host "> IP хоста:      $(if ($Ip) { $Ip } else { '<не определён>' })"
Write-Host "> server_base_url: $(if ($ServerUrl) { $ServerUrl } else { '<не построен>' })"
Write-Host ""

if ($DryRun) {
    Write-Host "ℹ️  -DryRun: config.json не изменён."
    exit 0
}

# ── Замена значения одного ключа, сохраняя комментарии JSONC ────────
$text = Get-Content -Path $Config -Raw -Encoding UTF8

function Set-Key {
    param([string]$Key, [string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return }
    # JSON-экранирование значения: \ -> \\ и " -> \"
    $jsonVal = $Value -replace '\\', '\\' -replace '"', '\"'
    $pattern = '("' + [regex]::Escape($Key) + '"\s*:\s*")[^"]*(")'
    if ($script:text -match $pattern) {
        # $ в replacement-строке .NET Regex спецсимвол — удваиваем.
        $repl = '${1}' + ($jsonVal -replace '\$', '$$$$') + '${2}'
        $script:text = [regex]::Replace($script:text, $pattern, $repl)
    } else {
        Write-Warning "Ключ `"$Key`" не найден в config.json — пропущен."
    }
}

Set-Key 'wifi_ssid'       $Ssid
Set-Key 'wifi_password'   $Password
Set-Key 'server_base_url' $ServerUrl

# Пишем без BOM, чтобы Python json/strip_jsonc читал файл без сюрпризов.
[System.IO.File]::WriteAllText($Config, $text, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "✅ config.json обновлён."
if (-not $Ssid)      { Write-Warning "SSID не определён — проверь Wi-Fi и впиши вручную." }
if (-not $Password)  { Write-Warning "Пароль не найден — впиши esp.wifi_password вручную." }
if (-not $Ip)        { Write-Warning "IP не определён — впиши esp.server_base_url вручную." }

if ($Flash) {
    Write-Host ""
    Write-Host "> Перепрошиваю плату…"
    bash (Join-Path $RootDir 'esp/flash.sh')
}
