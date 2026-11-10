#!/usr/bin/env bash
# 02-watchdog.sh: configura el watchdog hardware de la Raspberry Pi.
# Anade un drop-in propio en /etc para que la politica de reinicio ante un
# cuelgue sea explicita y no dependa de los valores del fabricante.

set -euo pipefail

DROPIN_DIR="/etc/systemd/system.conf.d"
DROPIN="${DROPIN_DIR}/50-guard-watchdog.conf"

[[ $EUID -eq 0 ]] || { echo "ERROR: ejecutar con sudo" >&2; exit 1; }
[[ -e /dev/watchdog0 ]] || { echo "ERROR: /dev/watchdog0 no existe" >&2; exit 1; }

echo "[*] Watchdog detectado: $(cat /sys/class/watchdog/watchdog0/identity)"

mkdir -p "$DROPIN_DIR"
cat > "$DROPIN" <<'EOF'
# GUARD — politica de watchdog del dispositivo.
# Sustituye a 40-rpi-enable-watchdog.conf (prioridad de /etc sobre /usr/lib).
[Manager]
# Deteccion de cuelgue del sistema. systemd refresca a la mitad de este valor.
# 20s: compromiso entre reaccion en campo y falsos positivos bajo carga
# sostenida (revalidar tras el benchmark de la seccion 5).
RuntimeWatchdogSec=20s

# Si un apagado/reinicio ordenado se atasca, el HW fuerza el reinicio.
RebootWatchdogSec=60s

EOF

echo "[*] Drop-in escrito en $DROPIN"

# Los cambios de watchdog requieren re-ejecutar PID 1 (no basta daemon-reload).
systemctl daemon-reexec

# ServiceWatchdogs no es clave de system.conf: se gestiona en runtime.
# Por defecto ya esta activo; se deja explicito por trazabilidad.
systemctl service-watchdogs yes

echo "[*] Configuracion efectiva:"
systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec -p ServiceWatchdogs
echo "[*] Estado del hardware:"
grep -H . /sys/class/watchdog/watchdog0/{timeout,timeleft,state,bootstatus}
