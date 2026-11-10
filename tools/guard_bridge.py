#!/usr/bin/env python3
"""guard_bridge: puente entre el detector y el ESP32 por la UART.
Envia latidos, telemetria y detecciones con histeresis, y publica para el
panel las pulsaciones del joystick que llegan del ESP32.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import serial

PUERTO_DEF = "/dev/ttyAMA3"
BAUD = 115200

INTERVALO_HB = 2.0
INTERVALO_SYS = 10.0
HISTERESIS_DEF = 10.0

HEALTH_FILE = Path("/run/guard/detector.health")
UMBRAL_HEALTH = 30.0          # seccion 3.3
UNIDAD_DETECTOR = "guard-detector-rf.service"
# El clasificador de UAV publica sus alarmas con el mismo formato de evento
# que el detector ("detection"), asi que el puente las trata igual: una sola
# via hacia el ESP32 y una sola histeresis para no pitar dos veces.
UNIDAD_CLASIFICADOR = "guard-clasificador.service"

# Pulsaciones del joystick para el panel. Vive en /run, que es tmpfs: no
# desgasta la tarjeta y se limpia sola en cada arranque.
ENTRADA_FILE = Path("/run/guard/input.json")
ACCIONES = ("arriba", "abajo", "ok", "atras")

_parar = threading.Event()
_lock_serie = threading.Lock()


# Tramas

def checksum(cuerpo: str) -> str:
    """Calcula la suma de control de una trama: XOR de sus bytes.

    @param  cuerpo  texto de la trama sin prefijo ni terminador
    @return suma de control en hexadecimal de 2 digitos
    """
    cs = 0
    for b in cuerpo.encode():
        cs ^= b
    return f"{cs:02X}"


def enviar(ser: serial.Serial, cuerpo: str) -> None:
    """Envia una trama de la Pi al ESP32 con prefijo, suma de control
    y terminador.

    @param  ser     puerto serie abierto
    @param  cuerpo  contenido de la trama (tipo y campos)
    """
    trama = f">{cuerpo}*{checksum(cuerpo)}\n"
    with _lock_serie:
        ser.write(trama.encode())


def descomponer(texto: str) -> list:
    """Valida una trama del ESP32 y la separa en campos. Las tramas con
    suma de control incorrecta se descartan.

    @param  texto  linea recibida del ESP32
    @return lista de campos, o lista vacia si la trama no es valida
    """
    if not texto.startswith("<") or "*" not in texto:
        return []
    cuerpo, _, cs_recibido = texto[1:].rpartition("*")
    if checksum(cuerpo) != cs_recibido.strip().upper():
        return []
    # El protocolo cierra la lista de campos con un separador final.
    if cuerpo.endswith("|"):
        cuerpo = cuerpo[:-1]
    return cuerpo.split("|")


# Pulsaciones

def _seq_inicial() -> int:
    """Lee el ultimo numero de pulsacion publicado para continuar la
    numeracion tras un reinicio del puente.

    @return ultimo numero de secuencia, o 0 si no hay fichero
    """
    try:
        with open(ENTRADA_FILE) as f:
            return int(json.load(f).get("seq", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


_seq = _seq_inicial()


def publicar_pulsacion(accion: str) -> None:
    """Publica una pulsacion del joystick para el panel, escribiendo el
    fichero de forma atomica (temporal y renombrado).

    @param  accion  accion del joystick (arriba, abajo, ok o atras)
    """
    global _seq
    _seq += 1
    datos = {
        "seq": _seq,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
              + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
        "accion": accion,
    }
    try:
        ENTRADA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ENTRADA_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(datos, f, separators=(",", ":"))
        os.replace(tmp, ENTRADA_FILE)
    except OSError as exc:
        print(f"[btn] no se pudo publicar la pulsacion: {exc}",
              file=sys.stderr, flush=True)
        return
    print(f"[btn] {accion} (seq {_seq})", flush=True)


# Estado salud

def estado_detector(unidad: str) -> str:
    """Determina el estado del detector a partir de su unidad systemd y
    de la antiguedad de su fichero de latido.

    @param  unidad  nombre de la unidad systemd del detector
    @return 'OK', 'DEGRADED' o 'ERROR'
    """
    try:
        activo = subprocess.run(
            ["systemctl", "is-active", "--quiet", unidad],
            timeout=2,
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        activo = False

    if not activo:
        return "ERROR"

    try:
        edad = time.time() - HEALTH_FILE.stat().st_mtime
    except OSError:
        return "DEGRADED"

    return "OK" if edad <= UMBRAL_HEALTH else "DEGRADED"


# Hilos

def hilo_latido(ser: serial.Serial, inicio: float, unidad: str) -> None:
    """Envia el latido HB al ESP32 cada 2 s con el tiempo encendido y el
    estado del detector.

    @param  ser     puerto serie abierto
    @param  inicio  instante de arranque del puente (time.monotonic)
    @param  unidad  unidad systemd del detector
    """
    while not _parar.is_set():
        uptime = int(time.monotonic() - inicio)
        enviar(ser, f"HB|{uptime}|{estado_detector(unidad)}|")
        _parar.wait(INTERVALO_HB)


def _leer_primer_valor(ruta: str, idx: int = 0) -> float:
    """Lee un valor numerico de un fichero de texto del sistema.

    @param  ruta  ruta del fichero
    @param  idx   posicion del valor en la primera linea
    @return valor leido
    """
    with open(ruta) as f:
        return float(f.read().split()[idx])


def hilo_telemetria(ser: serial.Serial) -> None:
    """Envia cada 10 s la telemetria SYS: uso de CPU, temperatura y
    memoria usada.

    @param  ser  puerto serie abierto
    """
    prev = None
    while not _parar.is_set():
        try:
            with open("/proc/stat") as f:
                campos = [float(x) for x in f.readline().split()[1:]]
            total, ocioso = sum(campos), campos[3]
            cpu = 0
            if prev:
                dt, di = total - prev[0], ocioso - prev[1]
                if dt > 0:
                    cpu = int(100 * (dt - di) / dt)
            prev = (total, ocioso)

            temp = int(_leer_primer_valor(
                "/sys/class/thermal/thermal_zone0/temp") / 1000)

            with open("/proc/meminfo") as f:
                mem = {}
                for linea in f:
                    k, v = linea.split(":", 1)
                    mem[k] = float(v.split()[0])
            usada = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])

            enviar(ser, f"SYS|{cpu}|{temp}|{int(usada)}|")
        except (OSError, ValueError, KeyError, IndexError) as exc:
            print(f"[telemetria] omitida: {exc}", file=sys.stderr)

        _parar.wait(INTERVALO_SYS)


def hilo_respuestas(ser: serial.Serial) -> None:
    """Atiende las tramas del ESP32: publica las pulsaciones para el panel
    y registra los acuses y los errores.

    @param  ser  puerto serie abierto
    """
    buf = b""
    while not _parar.is_set():
        try:
            datos = ser.read(64)
        except serial.SerialException:
            break
        if not datos:
            continue
        buf += datos
        while b"\n" in buf:
            linea, buf = buf.split(b"\n", 1)
            texto = linea.decode(errors="replace").strip()
            if not texto:
                continue

            campos = descomponer(texto)
            if campos and campos[0] == "BTN":
                accion = campos[1] if len(campos) > 1 else ""
                if accion in ACCIONES:
                    publicar_pulsacion(accion)
                else:
                    # Una accion desconocida no se propaga: el panel solo
                    # entiende cuatro, y reenviarle cualquier cosa que
                    # llegue por el cable es superficie de fallo gratuita.
                    print(f"[btn] accion no reconocida: {accion!r}",
                          flush=True)
                continue

            print(f"[esp32] {texto}", flush=True)


# Eventos

def seguir_eventos(unidades: list):
    """Lee en continuo del diario del sistema los eventos JSON de las
    unidades indicadas.

    @param  unidades  lista de unidades systemd a seguir
    @return generador de eventos (diccionarios)
    """
    cmd = ["journalctl"]
    for u in unidades:
        cmd += ["-u", u]
    proc = subprocess.Popen(
        cmd + ["-f", "-n", "0", "-o", "cat"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        for linea in proc.stdout:
            linea = linea.strip()
            if not linea.startswith("{"):
                continue
            try:
                yield json.loads(linea)
            except json.JSONDecodeError:
                continue
    finally:
        proc.terminate()


def main() -> int:
    """Abre el puerto serie, lanza los hilos y reenvia al ESP32 las
    detecciones confirmadas, aplicando la histeresis.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Puente detector -> interfaz fisica")
    p.add_argument("--puerto", default=PUERTO_DEF)
    p.add_argument("--unidad", default=UNIDAD_DETECTOR)
    p.add_argument("--clasificador", default=UNIDAD_CLASIFICADOR,
                   help="unidad del clasificador de UAV ('' para no seguirla)")
    p.add_argument("--histeresis", type=float, default=HISTERESIS_DEF,
                   help="segundos sin re-alertar dentro del mismo episodio")
    args = p.parse_args()

    try:
        ser = serial.Serial(args.puerto, BAUD, timeout=0.5)
    except serial.SerialException as exc:
        print(f"ERROR: no se pudo abrir {args.puerto}: {exc}", file=sys.stderr)
        return 1

    inicio = time.monotonic()
    print(f"puente iniciado: {args.puerto} @ {BAUD}, unidad {args.unidad}",
          flush=True)
    print(f"pulsaciones -> {ENTRADA_FILE} (desde seq {_seq})", flush=True)

    threading.Thread(target=hilo_latido, args=(ser, inicio, args.unidad),
                     daemon=True).start()
    for destino in (hilo_telemetria, hilo_respuestas):
        threading.Thread(target=destino, args=(ser,), daemon=True).start()

    ultima_alerta = 0.0
    suprimidas = 0

    try:
        unidades = [args.unidad] + ([args.clasificador]
                                    if args.clasificador else [])
        for ev in seguir_eventos(unidades):
            if ev.get("type") != "detection" or not ev.get("confirmed"):
                continue

            ahora = time.monotonic()
            if ahora - ultima_alerta < args.histeresis:
                suprimidas += 1
                continue

            if suprimidas:
                print(f"[histeresis] {suprimidas} detecciones del episodio "
                      f"anterior no re-alertaron", flush=True)
                suprimidas = 0

            rssi = ev.get("rssi_dbfs")
            modelo = ev.get("model") or "-"
            conf = ev.get("confidence", 0.0)

            campo_rssi = "-" if rssi is None else f"{rssi:.1f}"
            enviar(ser, f"DET|{campo_rssi}|{modelo}|{conf:.2f}|")
            ultima_alerta = ahora
            print(f"[alerta] {modelo} {campo_rssi} conf={conf:.2f}", flush=True)

    except KeyboardInterrupt:
        pass
    finally:
        _parar.set()
        time.sleep(0.3)
        ser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
