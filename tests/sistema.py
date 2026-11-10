"""sistema: pruebas de sistema que provocan fallos reales en el equipo
(bloqueo o caida del detector, receptor desconectado) y miden la recuperacion.
"""

import json
import os
import signal
import time
from pathlib import Path

from comun import (PASS, FAIL, SKIP, MANUAL, Resultado, ejecutar, activo,
                   propiedad, pid_principal, esperar, leer_snapshot,
                   edad_snapshot, unidad_detector, esperar_detector_listo, UNIDAD_RF, UNIDAD_PUENTE)

HACKRF_VID, HACKRF_PID = "1d50", "6089"


def _a_segundos(valor: str) -> float:
    """Convierte una duracion de systemd ('30s', '1min 30s', '0') a segundos.

    @param  valor  duracion en el formato de systemd
    @return duracion en segundos
    """
    if not valor or valor in ("0", "infinity"):
        return 0.0
    total = 0.0
    for parte in valor.split():
        for sufijo, factor in (("min", 60), ("ms", 0.001), ("us", 1e-6), ("s", 1), ("h", 3600)):
            if parte.endswith(sufijo):
                try:
                    total += float(parte[: -len(sufijo)]) * factor
                except ValueError:
                    pass
                break
    return total


def s1_configuracion() -> Resultado:
    """S1: comprueba la configuracion efectiva de los watchdogs y del
    reinicio automatico.

    @return Resultado de la prueba
    """
    unidad = unidad_detector() or UNIDAD_RF
    hw = _a_segundos(ejecutar(["systemctl", "show", "-p", "RuntimeWatchdogUSec",
                               "--value"]).stdout.strip())
    disp = Path("/dev/watchdog").exists() or Path("/dev/watchdog0").exists()
    restart = propiedad(unidad, "Restart")
    wd_servicio = _a_segundos(propiedad(unidad, "WatchdogUSec"))
    memoria = propiedad(unidad, "MemoryMax")
    cuota = propiedad(unidad, "CPUQuotaPerSecUSec")

    ok = hw > 0 and disp and restart in ("always", "on-failure")
    detalle = (f"watchdog hardware: {hw:.0f} s ({'dispositivo presente' if disp else 'sin dispositivo'}); "
               f"{unidad}: Restart={restart}, WatchdogSec={wd_servicio:.0f} s, "
               f"MemoryMax={memoria}, CPUQuota={cuota or 'sin límite'}")
    if wd_servicio == 0:
        detalle += (". AVISO: el detector no tiene watchdog de servicio; un bloqueo "
                    "sin terminar el proceso no se detecta (ver S2)")
    return Resultado(PASS if ok else FAIL, detalle)


def s2_bloqueo_detector() -> Resultado:
    """S2: detiene el proceso del detector sin terminarlo (SIGSTOP) y mide
    el tiempo hasta su reinicio.

    @return Resultado de la prueba
    """
    unidad = unidad_detector()
    if not unidad:
        return Resultado(SKIP, "no hay ningún detector en marcha")
    pid = pid_principal(unidad)
    if not pid:
        return Resultado(SKIP, "no se pudo leer el PID del detector")

    os.kill(pid, signal.SIGSTOP)
    try:
        t = esperar(lambda: activo(unidad) and pid_principal(unidad) not in (0, pid), 90)
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        if not activo(unidad) or pid_principal(unidad) == pid:
            ejecutar(["systemctl", "restart", unidad])

    if t is None:
        return Resultado(FAIL, f"{unidad}: el proceso {pid} quedó detenido 90 s sin que "
                               "systemd lo reiniciara (reiniciado a mano al terminar)")
    return Resultado(PASS, f"{unidad}: proceso {pid} detenido; systemd lo sustituyó por "
                           f"el {pid_principal(unidad)} en {t:.0f} s")


def s3_terminacion_detector() -> Resultado:
    """S3: termina el proceso del detector (SIGKILL) y mide el tiempo hasta
    su reinicio.

    @return Resultado de la prueba
    """
    unidad = unidad_detector()
    if not unidad:
        return Resultado(SKIP, "no hay ningún detector en marcha")
    pid = pid_principal(unidad)
    if not pid:
        return Resultado(SKIP, "no se pudo leer el PID del detector")
    os.kill(pid, signal.SIGKILL)
    t = esperar(lambda: activo(unidad) and pid_principal(unidad) not in (0, pid), 30)
    if t is None:
        ejecutar(["systemctl", "restart", unidad])
        return Resultado(FAIL, f"{unidad}: no se reinició en 30 s (reiniciado a mano)")
    return Resultado(PASS, f"{unidad}: proceso {pid} terminado; reiniciado en {t:.1f} s")


def _dispositivo_hackrf() -> str:
    """Busca el HackRF en el bus USB.

    @return nombre del dispositivo en /sys/bus/usb, o vacio si no esta
    """
    for d in Path("/sys/bus/usb/devices").iterdir():
        try:
            if ((d / "idVendor").read_text().strip() == HACKRF_VID
                    and (d / "idProduct").read_text().strip() == HACKRF_PID):
                return d.name
        except OSError:
            continue
    return ""


def s4_receptor_sin_datos() -> Resultado:
    """S4: desconecta el HackRF del bus USB por software y mide la
    deteccion del fallo y la recuperacion.

    @return Resultado de la prueba
    """
    if not activo(UNIDAD_RF):
        return Resultado(SKIP, "el detector real no está en marcha")
    disp = _dispositivo_hackrf()
    if not disp:
        return Resultado(SKIP, "no se encuentra el HackRF en el bus USB")
    motivo = esperar_detector_listo()
    if motivo:
        return Resultado(SKIP, motivo)

    Path("/sys/bus/usb/drivers/usb/unbind").write_text(disp)
    try:
        t_fallo = esperar(lambda: leer_snapshot().get("receptor") == "sin_datos", 90)
    finally:
        Path("/sys/bus/usb/drivers/usb/bind").write_text(disp)

    t_rec = esperar(lambda: leer_snapshot().get("receptor") != "sin_datos"
                    and edad_snapshot() < 10, 120)
    reinicio = False
    if t_rec is None:
        ejecutar(["systemctl", "restart", UNIDAD_RF])
        reinicio = True

    if t_fallo is None:
        return Resultado(FAIL, "el detector no publicó receptor=sin_datos en 90 s")
    rec = (f"se recuperó solo en {t_rec:.0f} s al volver a conectarlo" if t_rec is not None
           else "no se recuperó solo; hizo falta reiniciar el servicio")
    return Resultado(PASS, f"estado sin_datos publicado {t_fallo:.0f} s después de la "
                           f"desconexión; {rec}. Comprobar a la vista: panel en rojo",
                     {"reinicio_manual": reinicio})


def s5_latencia_alerta() -> Resultado:
    """S5: mide en el diario el tiempo entre el envio de DET y el ACK del
    ESP32.

    @return Resultado de la prueba
    """
    salida = ejecutar(["journalctl", "-u", UNIDAD_PUENTE, "-o", "json",
                       "--since", "-7d", "--no-pager"], timeout=60)
    alertas, latencias = [], []
    for linea in salida.stdout.splitlines():
        try:
            ev = json.loads(linea)
        except json.JSONDecodeError:
            continue
        msg = ev.get("MESSAGE", "")
        if not isinstance(msg, str):
            continue
        t = int(ev.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        if msg.startswith("[alerta]"):
            alertas.append(t)
        elif "ACK|DET" in msg and alertas:
            d = t - alertas[-1]
            if 0 <= d < 2:
                latencias.append(d * 1000)
                alertas.pop()
    if not latencias:
        return Resultado(SKIP, "no hay alertas con confirmación en el diario de los "
                               "últimos 7 días (provocar una detección y repetir)")
    latencias.sort()
    mediana = latencias[len(latencias) // 2]
    return Resultado(PASS, f"{len(latencias)} alertas; latencia DET->ACK mediana "
                           f"{mediana:.0f} ms, máxima {latencias[-1]:.0f} ms "
                           "(incluye el registro en el diario)",
                     {"latencias_ms": latencias})


def s6_cuelgue_sistema() -> Resultado:
    """S6: prueba manual de fallo del nucleo; devuelve el procedimiento.

    @return Resultado MANUAL con el procedimiento
    """
    return Resultado(MANUAL, "no se automatiza porque reinicia el equipo. "
                             "Procedimiento: echo c | sudo tee /proc/sysrq-trigger; "
                             "anotar el paso del ESP32 a LINK_DOWN (LED rojo) y el "
                             "tiempo hasta que el equipo responde de nuevo")


PRUEBAS = [
    ("S1", "Configuración de watchdogs y reinicio automático", s1_configuracion, True),
    ("S2", "Detector bloqueado (SIGSTOP)", s2_bloqueo_detector, True),
    ("S3", "Detector terminado (SIGKILL)", s3_terminacion_detector, True),
    ("S4", "Receptor desconectado: estado sin datos", s4_receptor_sin_datos, True),
    ("S5", "Latencia de la alerta DET -> ACK", s5_latencia_alerta, True),
    ("S6", "Cuelgue del sistema (watchdog hardware)", s6_cuelgue_sistema, False),
]
