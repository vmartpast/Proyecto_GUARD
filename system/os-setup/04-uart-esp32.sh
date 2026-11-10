#!/usr/bin/env bash
# 04-uart-esp32.sh: habilita la uart3 para el enlace con el ESP32.
# Usa GPIO 4 (TX, pin fisico 7) y GPIO 5 (RX, pin fisico 29), sin chocar con
# el I2C, el SPI ni la consola serie. Ver docs/PROTOCOLO_UART.md.

set -euo pipefail

BOOT_CFG="/boot/firmware/config.txt"
MARKER="# --- GUARD ---"

[[ $EUID -eq 0 ]] || { echo "ERROR: ejecutar con sudo" >&2; exit 1; }
[[ -f "$BOOT_CFG" ]] || { echo "ERROR: no existe $BOOT_CFG" >&2; exit 1; }

echo "[1/2] Habilitando uart3"
if grep -q "^dtoverlay=uart3" "$BOOT_CFG"; then
  echo "      ya configurado"
else
  grep -q "$MARKER" "$BOOT_CFG" || printf '\n%s\n' "$MARKER" >> "$BOOT_CFG"
  echo "dtoverlay=uart3" >> "$BOOT_CFG"
  echo "      anadido a $BOOT_CFG"
fi

echo "[2/2] Estado actual (antes del reinicio)"
grep -nE "^(enable_uart|dtoverlay=uart|dtoverlay=disable-bt)" "$BOOT_CFG"
ls -l /dev/ttyAMA* 2>&1

echo
echo "=== Requiere reinicio. Tras el, debe existir /dev/ttyAMA1 ==="
