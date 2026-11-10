#!/usr/bin/env python3
"""uart_loopback_test: prueba aislada del puerto serie del enlace con el
ESP32. Con TX y RX puenteados, envia un mensaje y comprueba que vuelve igual.
"""
import sys
import time

import serial

PUERTO = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyAMA3"
BAUD = 115200
MSG = b"GUARD-LOOPBACK-OK\n"


def main() -> int:
    """Envia el mensaje de prueba por el puerto y compara lo recibido.

    @return 0 si el mensaje vuelve intacto, 1 si no
    """
    try:
        s = serial.Serial(PUERTO, BAUD, timeout=2)
    except serial.SerialException as exc:
        print(f"ERROR: no se pudo abrir {PUERTO}: {exc}")
        return 1

    with s:
        time.sleep(0.2)
        s.reset_input_buffer()
        s.write(MSG)
        time.sleep(0.3)
        r = s.read(len(MSG))

    print(f"puerto  : {PUERTO} @ {BAUD} 8N1")
    print(f"enviado : {MSG!r}")
    print(f"recibido: {r!r}")

    if r == MSG:
        print(">>> LOOPBACK CORRECTO")
        return 0
    if not r:
        print(">>> FALLO: sin respuesta. Revisar el puente entre pines 7 y 29.")
    else:
        print(">>> FALLO: datos alterados. Posible ruido o velocidad incorrecta.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
