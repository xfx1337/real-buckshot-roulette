# ==========================================
# ПОИСК ШЛЮЗА ADDPAC В ЛОКАЛЬНОЙ СЕТИ
# ==========================================
# Запусти этот скрипт в PowerShell. Он найдет
# IP-адрес твоего шлюза AddPac, даже если ты
# его не знаешь.

Write-Host "1. Определяем твою текущую подсеть..." -ForegroundColor Cyan
$ipConfig = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -ne $null } | Select-Object -First 1
if (-not $ipConfig) {
    Write-Host "Ошибка: Не найден активный сетевой адаптер с шлюзом. Ты подключен к роутеру?" -ForegroundColor Red
    exit
}

$myIp = $ipConfig.IPv4Address.IPAddress
$gateway = $ipConfig.IPv4DefaultGateway.NextHop
Write-Host "Твой IP: $myIp, Роутер: $gateway"

# Вычисляем базовую подсеть (например 192.168.1.)
$subnetBase = $myIp.Substring(0, $myIp.LastIndexOf('.') + 1)
Write-Host "Сканируем подсеть: ${subnetBase}X" -ForegroundColor Cyan

Write-Host "`n2. Отправляем пинги всем 254 адресам (чтобы обновить ARP-таблицу)..." -ForegroundColor Yellow
Write-Host "(Это займет около 10-15 секунд, жди...)"

# Быстрый асинхронный пинг (работает на стандартном Windows PowerShell 5.1)
1..254 | ForEach-Object { 
    Start-Job -ScriptBlock { 
        param($ip) 
        Test-Connection -Count 1 -Quiet -TimeoutSeconds 1 $ip 
    } -ArgumentList "${subnetBase}$_" | Out-Null
}
Get-Job | Wait-Job | Remove-Job

Write-Host "`n3. Ищем AddPac в ARP-таблице по его MAC-адресу (00-0B-44 или 00-11-E5)..." -ForegroundColor Cyan
$addpac = Get-NetNeighbor -AddressFamily IPv4 | Where-Object { $_.LinkLayerAddress -match '^00-0B-44|^00-11-E5' -and $_.State -ne 'Unreachable' }

if ($addpac) {
    Write-Host "`n[УСПЕХ] НАЙДЕН ШЛЮЗ ADDPAC!" -ForegroundColor Green
    foreach ($dev in $addpac) {
        $foundIp = $dev.IPAddress
        $foundMac = $dev.LinkLayerAddress
        Write-Host "IP-адрес: $foundIp" -ForegroundColor Green
        Write-Host "MAC-адрес: $foundMac" -ForegroundColor DarkGray
        
        Write-Host "Проверяю порты на $foundIp..."
        foreach ($p in 23,80,5060) {
            $test = Test-NetConnection $foundIp -Port $p -WarningAction SilentlyContinue
            if ($test.TcpTestSucceeded) {
                if ($p -eq 23) { Write-Host "  -> Порт 23 (Telnet) ОТКРЫТ" }
                if ($p -eq 80) { Write-Host "  -> Порт 80 (Web-админка) ОТКРЫТ" }
                if ($p -eq 5060) { Write-Host "  -> Порт 5060 (SIP-телефония) ОТКРЫТ" }
            }
        }
        Write-Host "`nЧТОБЫ ЗАЙТИ В НАСТРОЙКИ:" -ForegroundColor Yellow
        Write-Host "Открой браузер: http://$foundIp"
        Write-Host "Или подключись по Telnet: telnet $foundIp"
    }
} else {
    Write-Host "`n[НЕ НАЙДЕНО] AddPac не откликнулся в сети ${subnetBase}X." -ForegroundColor Red
    Write-Host "Возможные причины:"
    Write-Host "1. У него статический IP в ДРУГОЙ подсети (например, он в 192.168.0.X, а ты в 192.168.1.X)."
    Write-Host "2. Он не подключен к роутеру (проверь патч-корд)."
    Write-Host "3. Он выключен."
}

Write-Host "`nГотово. Нажми любую клавишу для выхода..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
