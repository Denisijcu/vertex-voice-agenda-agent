#!/bin/sh
# Rellena las plantillas con las variables del .env y arranca Asterisk en primer plano.
set -e

: "${HOST_IP:?Falta HOST_IP en telephony/.env (la IP de tu PC que verá el celular)}"
: "${SIP_USER:=denis}"
: "${SIP_PASSWORD:?Falta SIP_PASSWORD en telephony/.env}"
: "${GATEWAY_ADDR:=host.docker.internal:9092}"

# AudioSocket necesita IP:puerto; se resuelve el nombre del host (p. ej. host.docker.internal).
gw_host="${GATEWAY_ADDR%:*}"
gw_port="${GATEWAY_ADDR##*:}"
# Primero IPv4 (Docker Desktop puede devolver IPv6 para host.docker.internal). Si solo hay IPv6, va entre
# corchetes: AudioSocket necesita [dirección]:puerto para saber dónde termina la dirección.
gw_ip="$(getent ahostsv4 "$gw_host" | awk '{print $1; exit}')"
if [ -z "$gw_ip" ]; then
  gw_ip="$(getent hosts "$gw_host" | awk '{print $1; exit}')"
fi
case "$gw_ip" in
  *:*) gw_ip="[$gw_ip]" ;;
esac
GATEWAY_IP_PORT="${gw_ip:-$gw_host}:${gw_port}"

# IP propia del contenedor. Asterisk solo reescribe la dirección del AUDIO (c= del SDP) a HOST_IP si su propia
# IP está dentro de local_net; por eso local_net es exactamente esta IP (/32). El teléfono, que llega desde la
# puerta de enlace de Docker (.1), sigue siendo externo y la señalización también se reescribe.
CONTAINER_IP="$(hostname -i | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"
: "${CONTAINER_IP:?No pude averiguar la IP del contenedor}"

export HOST_IP SIP_USER SIP_PASSWORD GATEWAY_IP_PORT CONTAINER_IP
echo "[vva] HOST_IP=$HOST_IP  SIP_USER=$SIP_USER  pasarela=$GATEWAY_IP_PORT  contenedor=$CONTAINER_IP"

envsubst '${HOST_IP} ${SIP_USER} ${SIP_PASSWORD} ${CONTAINER_IP}' < /opt/vva/templates/pjsip.conf > /etc/asterisk/pjsip.conf
envsubst '${GATEWAY_IP_PORT}' < /opt/vva/templates/extensions.conf > /etc/asterisk/extensions.conf
cp /opt/vva/templates/rtp.conf /etc/asterisk/rtp.conf
cp /opt/vva/templates/modules.conf /etc/asterisk/modules.conf

exec asterisk -f -vvv
