#!/bin/bash
# 05-rtc.sh: configura el reloj de tiempo real DS3231 por I2C, para que
# el equipo arranque con la hora correcta sin red. Es idempotente.
#
# Uso, en la Raspberry:
#     sudo bash 05-rtc.sh              configura (hace falta reiniciar)
#     sudo bash 05-rtc.sh --verificar  comprueba el RTC tras el reinicio

set -euo pipefail

CONFIG="${GUARD_CONFIG_TXT:-/boot/firmware/config.txt}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ejecutalo con sudo: sudo bash $0 ${1:-}"; exit 1
fi

# Verificacion

# Comprueba tras el reinicio que el RTC esta registrado, que el bus I2C
# lo ve y que la hora del sistema y la del RTC coinciden.
verificar() {
    local ok=1

    echo "== Dispositivo"
    if [ -e /dev/rtc0 ]; then
        echo "  /dev/rtc0: $(cat /sys/class/rtc/rtc0/name 2>/dev/null || echo '?')"
    else
        echo "  FALTA /dev/rtc0: revisar cableado (SDA pin 3, SCL pin 5, 3,3 V, GND)"
        echo "  y que config.txt tenga dtparam=i2c_arm=on y dtoverlay=i2c-rtc,ds3231"
        exit 1
    fi

    echo "== Bus I2C (0x68 debe aparecer como UU: ocupado por el controlador)"
    if command -v i2cdetect >/dev/null; then
        local fila
        fila=$(i2cdetect -y 1 | grep -E '^60:' || true)
        if [[ "$fila" == *UU* ]]; then
            echo "  0x68: UU"
        else
            echo "  0x68 no aparece como UU"; ok=0
        fi
    else
        echo "  i2cdetect no instalado (sudo apt install i2c-tools); se omite"
    fi

    echo "== Hora"
    local rtc sistema dif
    rtc=$(hwclock -r)
    echo "  RTC:     $rtc"
    echo "  sistema: $(date '+%Y-%m-%d %H:%M:%S%:z')"
    dif=$(( $(date +%s) - $(date -d "$rtc" +%s) ))
    echo "  diferencia sistema - RTC: ${dif#-} s"

    echo "== Sincronizacion"
    local sincronizado
    sincronizado=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo no)
    if [ "$sincronizado" = "yes" ]; then
        # Con la hora del sistema corregida por NTP, se guarda en el RTC.
        hwclock --systohc
        echo "  sistema sincronizado por NTP: hora guardada en el RTC"
    else
        echo "  sistema sin sincronizar: la hora viene del RTC (no se escribe)"
    fi

    echo "== Pila del modulo"
    # El controlador avisa con "SET TIME!" cuando el DS3231 arranca con el
    # oscilador parado: la hora no es fiable y suele indicar que la pila
    # no esta puesta o se ha agotado. Una vez tras instalar el modulo es
    # normal; en cada arranque, no.
    local avisos
    avisos=$(journalctl -k -b 0 2>/dev/null | grep -c "SET TIME" || true)
    if [ "${avisos:-0}" -gt 0 ]; then
        echo "  AVISO: el RTC arranco con el oscilador parado en este arranque"
        echo "  (revisar la pila CR2032 si se repite en los siguientes)"
    else
        echo "  sin avisos del oscilador en este arranque"
    fi

    echo
    if [ "$ok" -eq 1 ]; then
        echo "RTC operativo."
    else
        echo "Revisar los puntos marcados."
        return 1
    fi
}

if [ "${1:-}" = "--verificar" ]; then
    verificar
    exit 0
fi

# Configuracion
[ -f "$CONFIG" ] || { echo "No existe $CONFIG"; exit 1; }

cambios=0
copia_hecha=0
# Guarda una copia de config.txt la primera vez que se va a modificar.
copia() {
    if [ "$copia_hecha" -eq 0 ]; then
        cp -a "$CONFIG" "$CONFIG.antes-05-rtc.$(date +%Y%m%d-%H%M%S)"
        copia_hecha=1
    fi
}

# Anade una seccion [all] al final de config.txt si hace falta, para
# que las lineas nuevas no queden dentro de un filtro de modelo.
asegurar_all() {
    local ultima
    ultima=$(grep -E '^\s*\[' "$CONFIG" | tail -1 | tr -d '[:space:]' || true)
    if [ -n "$ultima" ] && [ "$ultima" != "[all]" ]; then
        printf '\n[all]\n' >> "$CONFIG"
    fi
}

echo "== $CONFIG"
if grep -Eq '^\s*dtparam=i2c_arm=on\b' "$CONFIG"; then
    echo "  dtparam=i2c_arm=on: ya estaba"
else
    copia
    sed -i -E 's/^(\s*dtparam=i2c_arm=off)/#\1/' "$CONFIG"
    asegurar_all
    echo "dtparam=i2c_arm=on" >> "$CONFIG"
    echo "  dtparam=i2c_arm=on: anadido"
    cambios=1
fi

if grep -Eq '^\s*dtoverlay=i2c-rtc,ds3231\b' "$CONFIG"; then
    echo "  dtoverlay=i2c-rtc,ds3231: ya estaba"
else
    copia
    asegurar_all
    echo "dtoverlay=i2c-rtc,ds3231" >> "$CONFIG"
    echo "  dtoverlay=i2c-rtc,ds3231: anadido"
    cambios=1
fi

echo "== fake-hwclock"
if dpkg -s fake-hwclock >/dev/null 2>&1; then
    apt-get purge -y fake-hwclock
    echo "  desinstalado"
else
    echo "  no instalado"
fi

echo "== RTC en UTC"
if command -v timedatectl >/dev/null; then
    if timedatectl set-local-rtc 0 2>/dev/null; then
        echo "  $(timedatectl | grep 'RTC in local TZ' | sed 's/^ *//')"
    else
        echo "  no se pudo fijar con timedatectl; revisar con: timedatectl"
    fi
fi

echo "== i2c-tools (para la verificacion)"
if command -v i2cdetect >/dev/null; then
    echo "  instalado"
else
    apt-get install -y i2c-tools >/dev/null 2>&1 \
        && echo "  instalado" \
        || echo "  no se pudo instalar (sin red?); la verificacion lo omitira"
fi

echo
if [ "$cambios" -eq 1 ]; then
    echo "Hecho. Reinicia y comprueba:"
    echo "    sudo reboot"
    echo "    sudo bash $0 --verificar"
else
    echo "La configuracion ya estaba completa. Comprobacion:"
    echo
    verificar
fi
