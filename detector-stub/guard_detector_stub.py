#!/usr/bin/env python3
"""guard_detector_stub: detector simulado que cumple la interfaz del detector
real (eventos JSON, latido y watchdog). Permite probar la cadena de aviso
sin receptor, con cinco modos de funcionamiento.
"""

import argparse
import json
import os
import random
import signal
import socket
import threading
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEALTH_FILE = Path("/run/guard/detector.health")
HEARTBEAT_INTERVAL = 5.0        # seccion 3.3 exige <= 10 s
STATUS_INTERVAL = 30.0

MODELS = ["dji_mini3", "dji_mavic3", "autel_evo2", None]

_running = True
_stop_event = None  # threading.Event, inicializado en main()


def _stop(signum, frame):
    """Detiene el detector simulado al recibir SIGTERM o SIGINT.

    @param  signum  senal recibida
    @param  frame   marco de pila actual (no se usa)
    """
    global _running
    _running = False
    if _stop_event is not None:
        _stop_event.set()


def now_iso() -> str:
    """Devuelve la fecha y hora actuales en UTC con milisegundos.

    @return marca de tiempo en formato ISO 8601
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def emit(obj: dict) -> None:
    """Escribe un evento JSON por la salida estandar, que recoge el diario
    del sistema.

    @param  obj  evento a emitir
    """
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit_status(mode: str, extra: dict | None = None) -> None:
    """Emite un evento de estado con el modo de funcionamiento.

    @param  mode   modo del detector simulado
    @param  extra  campos adicionales del evento, o None
    """
    ev = {"ts": now_iso(), "type": "status", "mode": mode}
    if extra:
        ev.update(extra)
    emit(ev)


def emit_detection(confirmed: bool = True) -> None:
    """Emite una deteccion simulada con modelo, confianza y potencia
    aleatorios.

    @param  confirmed  si la deteccion se marca como confirmada
    """
    emit({
        "ts": now_iso(),
        "type": "detection",
        "confirmed": confirmed,
        "label": "drone",
        "model": random.choice(MODELS),
        "confidence": round(random.uniform(0.72, 0.98), 2),
        "window_s": 0.1,
        "bursts": random.randint(8, 22),
        "rssi_dbfs": round(random.uniform(-78.0, -31.0), 1),
    })


def sd_notify(state: str) -> None:
    """Envia una notificacion a systemd por su socket, sin depender de
    python3-systemd. Sin systemd no hace nada.

    @param  state  mensaje de estado (READY=1, WATCHDOG=1...)
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):          # socket abstracto
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass


def touch_health() -> None:
    """Actualiza el fichero de latido que vigila el puente."""
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.touch()
    except OSError as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"no se pudo actualizar heartbeat: {exc}"})


def run_idle(args) -> int:
    """Modo idle: solo estado y latido, sin detecciones.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    started = time.monotonic()
    emit_status("idle", {"pid": os.getpid()})
    sd_notify("READY=1")
    last_status = started

    while _running:
        touch_health()
        sd_notify("WATCHDOG=1")
        if time.monotonic() - last_status >= STATUS_INTERVAL:
            emit_status("idle", {"uptime_s": round(time.monotonic() - started)})
            last_status = time.monotonic()
        # Espera interrumpible: SIGTERM corta de inmediato, no tras el sleep.
        _stop_event.wait(HEARTBEAT_INTERVAL)

    sd_notify("STOPPING=1")
    emit_status("idle", {"stopping": True})
    return 0



def _heartbeat_loop(started: float, mode: str, stop: threading.Event) -> None:
    """Mantiene el latido y el watchdog de systemd hasta recibir la orden
    de parada.

    @param  started  instante de arranque
    @param  mode     modo del detector simulado
    @param  stop     evento que detiene el bucle
    """
    last_status = started
    while not stop.is_set():
        touch_health()
        sd_notify("WATCHDOG=1")
        if time.monotonic() - last_status >= STATUS_INTERVAL:
            emit_status(mode, {"uptime_s": round(time.monotonic() - started)})
            last_status = time.monotonic()
        stop.wait(HEARTBEAT_INTERVAL)


def run_sporadic(args) -> int:
    """Modo sporadic: una deteccion confirmada cada 30-120 s para probar la
    cadena de aviso completa.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    started = time.monotonic()
    emit_status("sporadic", {"pid": os.getpid()})
    sd_notify("READY=1")

    hb = threading.Thread(target=_heartbeat_loop,
                          args=(started, "sporadic", _stop_event), daemon=True)
    hb.start()

    while _running:
        espera = random.uniform(args.min_interval, args.max_interval)
        if _stop_event.wait(espera):
            break
        emit_detection(confirmed=True)

    sd_notify("STOPPING=1")
    emit_status("sporadic", {"stopping": True})
    return 0


def run_burst(args) -> int:
    """Modo burst: rafagas de detecciones en pocos segundos para probar la
    histeresis del aviso.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    started = time.monotonic()
    emit_status("burst", {"pid": os.getpid()})
    sd_notify("READY=1")

    hb = threading.Thread(target=_heartbeat_loop,
                          args=(started, "burst", _stop_event), daemon=True)
    hb.start()

    while _running:
        n = random.randint(4, 9)
        emit_status("burst", {"episodio": n})
        for _ in range(n):
            if not _running:
                break
            emit_detection(confirmed=True)
            if _stop_event.wait(random.uniform(0.3, 1.2)):
                break
        if _stop_event.wait(random.uniform(20.0, 45.0)):
            break

    sd_notify("STOPPING=1")
    emit_status("burst", {"stopping": True})
    return 0


def run_flaky(args) -> int:
    """Modo flaky: deja de enviar el latido o termina con error para probar
    el watchdog y el reinicio automatico.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    started = time.monotonic()
    emit_status("flaky", {"pid": os.getpid(), "fallo_en_s": args.fail_after})
    sd_notify("READY=1")

    while _running and (time.monotonic() - started) < args.fail_after:
        touch_health()
        sd_notify("WATCHDOG=1")
        _stop_event.wait(HEARTBEAT_INTERVAL)

    if not _running:
        return 0

    if random.random() < 0.5:
        emit({"ts": now_iso(), "type": "error",
              "msg": "fallo simulado: bloqueo, cesa el heartbeat"})
        # Sin heartbeat: systemd debe intervenir por WatchdogSec.
        while _running:
            _stop_event.wait(5.0)
        return 0

    emit({"ts": now_iso(), "type": "error",
          "msg": "fallo simulado: salida con codigo 1"})
    return 1


def run_load(args) -> int:
    """Modo load: reserva memoria y ocupa la CPU para probar los limites de
    recursos de la unidad.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    started = time.monotonic()
    emit_status("load", {"pid": os.getpid(), "ram_mb": args.ram_mb,
                         "hilos": args.cpu_threads})
    sd_notify("READY=1")

    lastre = bytearray(args.ram_mb * 1024 * 1024)
    for i in range(0, len(lastre), 4096):     # tocar paginas: RSS real, no virtual
        lastre[i] = 1
    emit_status("load", {"ram_reservada_mb": args.ram_mb})

    def quemar():
        """Ocupa un nucleo de CPU con calculo hasta la orden de parada."""
        while not _stop_event.is_set():
            x = 0
            for i in range(200000):
                x += i * i

    for _ in range(args.cpu_threads):
        threading.Thread(target=quemar, daemon=True).start()

    _heartbeat_loop(started, "load", _stop_event)

    del lastre
    sd_notify("STOPPING=1")
    emit_status("load", {"stopping": True})
    return 0


MODES = {
    "idle": run_idle,
    "sporadic": run_sporadic,
    "burst": run_burst,
    "flaky": run_flaky,
    "load": run_load,
}


def main() -> int:
    """Lee los argumentos y ejecuta el modo de funcionamiento elegido.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Stub del detector GUARD")
    p.add_argument("--mode", choices=sorted(MODES), default="idle",
                   help="modo de operacion (seccion 4)")
    p.add_argument("--min-interval", type=float, default=30.0,
                   help="modo sporadic: intervalo minimo entre detecciones (s)")
    p.add_argument("--max-interval", type=float, default=120.0,
                   help="modo sporadic: intervalo maximo entre detecciones (s)")
    p.add_argument("--fail-after", type=float, default=60.0,
                   help="modo flaky: segundos hasta el fallo simulado")
    p.add_argument("--ram-mb", type=int, default=300,
                   help="modo load: MB de RAM a reservar")
    p.add_argument("--cpu-threads", type=int, default=2,
                   help="modo load: hilos de carga de CPU")
    args = p.parse_args()

    global _stop_event
    _stop_event = threading.Event()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    return MODES[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
