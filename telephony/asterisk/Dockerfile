# Asterisk 20 (paquete oficial de Ubuntu 24.04) con AudioSocket. Sin imágenes de terceros.
FROM ubuntu:24.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends asterisk gettext-base \
 && rm -rf /var/lib/apt/lists/*

COPY templates/ /opt/vva/templates/
COPY entrypoint.sh /opt/vva/entrypoint.sh
RUN sed -i 's/\r$//' /opt/vva/entrypoint.sh && chmod +x /opt/vva/entrypoint.sh

EXPOSE 5060/udp 10000-10020/udp
ENTRYPOINT ["/opt/vva/entrypoint.sh"]
