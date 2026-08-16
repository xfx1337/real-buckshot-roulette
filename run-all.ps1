param (
    [switch]$NoVoip = $false,
    [switch]$NoCams = $false,
    [switch]$NoBrowser = $false
)

$ErrorActionPreference = "Stop"

# Ensure Docker and FFmpeg are in the PATH
$DockerPath = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
$FFmpegPath = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin"
if ((Test-Path $DockerPath) -and ($env:PATH -notlike "*$DockerPath*")) { $env:PATH += ";$DockerPath" }
if ((Test-Path $FFmpegPath) -and ($env:PATH -notlike "*$FFmpegPath*")) { $env:PATH += ";$FFmpegPath" }

Write-Host "========================================="
Write-Host " Buckshot Roulette IRL - Windows Startup"
Write-Host "========================================="

# 1. Start Asterisk in Docker (if not disabled)
if (-not $NoVoip) {
    Write-Host "-> Starting Asterisk (Docker)..."
    Push-Location voip
    try {
        docker compose up -d
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Asterisk failed to start. Is Docker Desktop running?"
        }
    } catch {
        Write-Warning "Docker command not found. Please restart your PC if you just installed Docker Desktop."
    }

    Write-Host "-> Generating sounds..."
    python scripts/sounds.py
    
    Write-Host "-> Starting Web PBX Panel (Port 8080)..."
    Start-Process -FilePath "python" -ArgumentList "scripts/web.py --host 0.0.0.0 --port 8080" -WindowStyle Minimized -PassThru
    Pop-Location
}

# 2. Start Game Server (and MediaMTX if needed)
Write-Host "-> Starting Game Server (Port 8000)..."
$RunArgs = @()
if ($NoCams) { $RunArgs += "--no-mediamtx" }
if ($NoBrowser) { $RunArgs += "--no-browser" }

# Start Game Server in the current console
python run.py @RunArgs
