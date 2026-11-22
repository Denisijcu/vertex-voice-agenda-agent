# abrir-firewall.ps1 — Abre en el firewall de Windows SOLO los puertos de la centralita y SOLO para tu red local.
# Correr UNA vez, en PowerShell como ADMINISTRADOR, desde E:\vertex-voice-agenda-agent\telephony :
#   .\abrir-firewall.ps1
# Para quitarlo después:  Remove-NetFirewallRule -Group "Vertex Voice Agenda"
$ErrorActionPreference = "Stop"
$grupo = "Vertex Voice Agenda"
Get-NetFirewallRule -Group $grupo -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "VVA SIP 5060/udp" -Group $grupo -Direction Inbound -Protocol UDP -LocalPort 5060 `
    -RemoteAddress LocalSubnet, 100.64.0.0/10 -Action Allow -Profile Any | Out-Null
New-NetFirewallRule -DisplayName "VVA RTP 10000-10020/udp" -Group $grupo -Direction Inbound -Protocol UDP `
    -LocalPort 10000-10020 -RemoteAddress LocalSubnet, 100.64.0.0/10 -Action Allow -Profile Any | Out-Null
Write-Host "Listo: SIP 5060/udp y RTP 10000-10020/udp abiertos solo para tu red local y Tailscale." -ForegroundColor Green
