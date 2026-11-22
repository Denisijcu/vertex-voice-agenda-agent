# llamar.ps1 - El agente llama a tu celular (Linphone).
# Correr desde E:\vertex-voice-agenda-agent\telephony :   .\llamar.ps1
# Necesita: Asterisk levantado (docker compose up) y la pasarela corriendo (python -m gateway).
# Espera hasta 30 s a que el celular este disponible (Avail) antes de llamar: Android duerme Linphone a ratos.
$ErrorActionPreference = "Stop"
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) { Write-Error "Falta telephony\.env (copia .env.example a .env)." }
$sipUser = (Get-Content $envFile | Where-Object { $_ -match '^\s*SIP_USER\s*=' } | Select-Object -First 1) -replace '^\s*SIP_USER\s*=\s*', ''
if (-not $sipUser) { $sipUser = "denis" }

$estado = docker inspect -f '{{.State.Running}}' vva-asterisk 2>$null
if ($estado -ne "true") { Write-Error "El contenedor vva-asterisk no esta corriendo. Lanza 'docker compose up --build' en otra ventana." }
if (-not (Get-NetTCPConnection -LocalPort 9092 -State Listen -ErrorAction SilentlyContinue)) {
    Write-Error "La pasarela no esta escuchando en el 9092. Lanza '.\venv\Scripts\python.exe -m gateway' en otra ventana."
}

Write-Host "Esperando a que '$sipUser' este disponible (abre Linphone en el celular)..." -ForegroundColor Yellow
$listo = $false
for ($i = 0; $i -lt 15; $i++) {
    $linea = docker exec vva-asterisk asterisk -rx "pjsip show contacts" | Select-String "Contact:\s+$sipUser/"
    if ($linea -and $linea -match '\bAvail\b') { $listo = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $listo) {
    Write-Error ("'$sipUser' no esta disponible. En el celular: Linphone abierto con la pantalla encendida, " +
                 "Tailscale activo, y sin optimizacion de bateria para los dos.")
}

Write-Host "Disponible. Llamando a $sipUser..." -ForegroundColor Green
docker exec vva-asterisk asterisk -rx "channel originate PJSIP/$sipUser extension agente@agente"
