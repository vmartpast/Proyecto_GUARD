#!/usr/bin/env bash
# 03-boot-trim.sh: reduce el tiempo de arranque de GUARD.
# Quita de la cadena de arranque los servicios que no hacen falta en un equipo
# embebido. No toca la red ni la consola serie. Es idempotente.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "ERROR: ejecutar con sudo" >&2; exit 1; }

echo "[1/2] cloud-init deshabilitado (aprovisionamiento propio via repo)"
# El metodo soportado es el fichero centinela; enmascarar las unidades
# sueltas deja generadores activos que siguen costando tiempo.
touch /etc/cloud/cloud-init.disabled
for u in cloud-init-main cloud-init-local cloud-init-network cloud-config cloud-final; do
  systemctl disable "${u}.service" 2>/dev/null || true
done

echo "[2/2] Estado"
ls -l /etc/cloud/cloud-init.disabled
systemctl is-enabled cloud-init-main.service 2>&1 || true

echo
echo "=== HECHO. Reiniciar y comparar con systemd-analyze ==="
