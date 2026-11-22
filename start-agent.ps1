# start-agent.ps1 — Abre dos ventanas de PowerShell visibles: API (8015) y UI (4200).
# Correr desde E:\vertex-voice-agenda-agent :   .\start-agent.ps1
# Antes de lanzar, comprueba que los puertos estén libres y avisa si LM Studio (1234) no responde.
$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$ApiDir = Join-Path $RootDir "api"
$UiDir = Join-Path $RootDir "ui"
$VenvPy = Join-Path $ApiDir "venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Error "Entorno virtual no encontrado en: $VenvPy"
}
if (-not (Test-Path (Join-Path $UiDir "package.json"))) {
    Write-Error "No se encontró package.json en: $UiDir"
}
if (-not (Test-Path (Join-Path $UiDir "node_modules"))) {
    Write-Error "Falta ui\node_modules. Corre 'npm ci' dentro de $UiDir antes de arrancar."
}
if (-not (Test-Path (Join-Path $ApiDir ".env"))) {
    Write-Warning "No hay api\.env: la API usará los valores por defecto (copia api\.env.example a api\.env)."
}

foreach ($puerto in 8015, 4200) {
    $ocupado = Get-NetTCPConnection -LocalPort $puerto -State Listen -ErrorAction SilentlyContinue
    if ($ocupado) {
        $proc = Get-Process -Id $ocupado[0].OwningProcess -ErrorAction SilentlyContinue
        Write-Error "El puerto $puerto ya está en uso por '$($proc.ProcessName)' (PID $($ocupado[0].OwningProcess)). Ciérralo antes de arrancar."
    }
}

if (-not (Get-NetTCPConnection -LocalPort 1234 -State Listen -ErrorAction SilentlyContinue)) {
    Write-Warning "LM Studio no está escuchando en el puerto 1234: la API arrancará, pero cada turno dará error 502 hasta que lo enciendas."
}

$ApiCmd = @"
Set-Location '$ApiDir'
& '$VenvPy' -m uvicorn app.main:app --host 127.0.0.1 --port 8015 --reload
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $ApiCmd

Start-Sleep -Seconds 2

$UiCmd = @"
Set-Location '$UiDir'
npx ng serve --host 127.0.0.1 --port 4200
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $UiCmd

Write-Host "Arrancando API (http://127.0.0.1:8015/docs) y UI (http://127.0.0.1:4200)..." -ForegroundColor Green
