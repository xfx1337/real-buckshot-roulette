<#
.SYNOPSIS
Script to update config and flash ESP32 (Receiver and Transmitter).
#>
$ErrorActionPreference = "Stop"

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "   Buckshot Roulette - ESP32 Flasher   " -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

$ssid = ""
try {
    $netshOutput = netsh wlan show interfaces
    foreach ($line in $netshOutput) {
        if ($line -match '^\s*(?:SSID|Profile)\s*:\s+(.+)$') {
            $ssid = $matches[1].Trim()
            break
        }
    }
} catch {}

$ip = ""
try {
    $ipObj = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { 
        $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notmatch '^169\.254\.' 
    } | Select-Object -First 1
    if ($ipObj) {
        $ip = $ipObj.IPAddress
    }
} catch {}

if ([string]::IsNullOrWhiteSpace($ssid)) { Write-Host "[WARNING] Could not determine current Wi-Fi SSID." -ForegroundColor Yellow }
else { Write-Host "[INFO] Current Wi-Fi network: $ssid" -ForegroundColor Green }

if ([string]::IsNullOrWhiteSpace($ip)) { Write-Host "[WARNING] Could not determine local IP address." -ForegroundColor Yellow }
else { Write-Host "[INFO] Local server IP: $ip" -ForegroundColor Green }

# Python script inline to update config safely
$pyScript = @"
import sys, re
ssid = sys.argv[1]
ip = sys.argv[2]
try:
    with open('config.json', 'r', encoding='utf-8') as f: text = f.read()
    if ssid: text = re.sub(r'(`"wifi_ssid`"\s*:\s*`")[^`"]*(`")', r'\g<1>' + ssid + r'\g<2>', text)
    if ip: text = re.sub(r'(`"server_base_url`"\s*:\s*`"http://)[^:/]+(:\d+)?/?(`")', r'\g<1>' + ip + r'\g<2>\g<3>', text)
    with open('config.json', 'w', encoding='utf-8') as f: f.write(text)
except Exception as e:
    print('Python Error:', e)
    sys.exit(1)
"@

Set-Content -Path "update_config_temp.py" -Value $pyScript -Encoding UTF8
python update_config_temp.py "$ssid" "$ip"
$pyExit = $LASTEXITCODE
Remove-Item "update_config_temp.py" -Force -ErrorAction SilentlyContinue

if ($pyExit -eq 0) { Write-Host "[INFO] config.json successfully updated." -ForegroundColor Green }
else { Write-Host "[ERROR] Failed to update config.json" -ForegroundColor Red; exit 1 }

Write-Host "`n[INFO] Generating config.h (esp\gen_config.py)..." -ForegroundColor Cyan
python esp\gen_config.py
if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] Error generating config.h" -ForegroundColor Red; exit 1 }

$usePio = $false
if (-not (Get-Command "arduino-cli" -ErrorAction SilentlyContinue)) {
    if (Get-Command "pio" -ErrorAction SilentlyContinue) {
        $usePio = $true
        Write-Host "`n[INFO] Using PlatformIO." -ForegroundColor Yellow
    } else {
        Write-Host "`n[ERROR] arduino-cli and pio not found!" -ForegroundColor Red
        exit 1
    }
}

function Get-ComPort {
    $ports = [System.IO.Ports.SerialPort]::GetPortNames()
    if ($ports.Count -eq 0) { return $null }
    if ($ports.Count -eq 1) { return $ports[0] }
    Write-Host "Found ports: $($ports -join ', ')" -ForegroundColor Yellow
    return Read-Host "Enter COM port"
}

while ($true) {
    Write-Host "`n=========================================" -ForegroundColor Cyan
    Write-Host "Select board to flash:"
    Write-Host "1 - Receiver (esp)"
    Write-Host "2 - Transmitter (esp_transmitter)"
    Write-Host "0 - Exit"

    $choice = Read-Host "Your choice"
    if ($choice -eq '0') { exit 0 }
    
    if ($choice -eq '1' -or $choice -eq '2') {
        $targetDir = if ($choice -eq '1') { "esp" } else { "esp_transmitter" }

        if ($usePio) {
            pio run -t upload -d $targetDir
        } else {
            $port = Get-ComPort
            if (-not $port) { Write-Host "[ERROR] COM port not found." -ForegroundColor Red; continue }
            $fqbn = "esp32:esp32:esp32"
            arduino-cli compile --fqbn $fqbn $targetDir
            if ($LASTEXITCODE -eq 0) {
                arduino-cli upload --fqbn $fqbn -p $port --upload-property "upload.speed=115200" $targetDir
            }
        }
        if ($LASTEXITCODE -eq 0) { Write-Host "`n[SUCCESS] Flashing complete!" -ForegroundColor Green }
    }
}
