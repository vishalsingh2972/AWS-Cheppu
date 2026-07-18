$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
$BACKEND = Join-Path $ROOT "backend"
$FRONTEND = Join-Path $ROOT "frontend"
$ENV_FILE = Join-Path $BACKEND ".env"

if (Test-Path $ENV_FILE) {
    Get-Content $ENV_FILE | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name  = $matches[1].Trim()
            $value = $matches[2].Trim()
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    Write-Host "[env] Loaded .env" -ForegroundColor Green
} else {
    Write-Host "[warn] No .env found - using existing environment" -ForegroundColor Yellow
}

$PORT = if ($env:PORT) { $env:PORT } else { "8000" }

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found. Install Python 3.11+ and add it to PATH."
    exit 1
}

Write-Host "[deps] Installing Python dependencies..." -ForegroundColor Cyan
Push-Location $BACKEND
pip install -r requirements.txt -q
Pop-Location

Write-Host "[backend] Starting FastAPI on port $PORT ..." -ForegroundColor Cyan
$backendJob = Start-Job -ScriptBlock {
    param($dir, $port)
    Set-Location $dir
    python -m uvicorn app:app --host 0.0.0.0 --port $port --reload 2>&1
} -ArgumentList $BACKEND, $PORT

Start-Sleep -Seconds 3

$indexHtml = Join-Path $FRONTEND "index.html"
if (Test-Path $indexHtml) {
    Write-Host "[frontend] Opening browser..." -ForegroundColor Cyan
    Start-Process $indexHtml
}

Write-Host "[ready] Backend: http://localhost:$PORT" -ForegroundColor Green
Write-Host "[ready] Health:  http://localhost:$PORT/health" -ForegroundColor Green
Write-Host "[ready] Debug:   http://localhost:$PORT/debug" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray

try {
    while ($true) {
        $output = Receive-Job $backendJob
        if ($output) { Write-Host $output }
        Start-Sleep -Milliseconds 500
    }
} finally {
    Stop-Job $backendJob
    Remove-Job $backendJob
    Write-Host "[stopped] VoiceOps AI shut down." -ForegroundColor Red
}
