"""comun: utilidades compartidas por la bateria de pruebas (rutas, servicios
systemd, espera de condiciones y lectura del estado del detector).
"""

import importlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # /opt/guard_device
TOOLS = BASE / "tools"
RESULTADOS = Path(__file__).resolve().parent / "resultados"
SNAPSHOT = Path("/run/guard/spectrum.json")
LISTA_BLANCA = BASE / "config" / "lista_blanca.json"

UNIDAD_RF = "guard-detector-rf.service"
UNIDAD_STUB = "guard-detector-stub.service"
UNIDAD_PUENTE = "guard-bridge.service"
UNIDAD_PANEL = "guard-panel.service"
UNIDAD_SILENTE = "guard-silent-mode.service"

PASS, FAIL, SKIP, MANUAL = "PASS", "FAIL", "SKIP", "MANUAL"


@dataclass
class Resultado:
    """Resultado de una prueba: estado (PASS, FAIL, SKIP o MANUAL), detalle
    y datos medidos.
    """
    estado: str
    detalle: str = ""
    datos: dict = field(default_factory=dict)


def importar(nombre: str):
    """Importa un modulo de tools/ sin ejecutar su main().

    @param  nombre  nombre del modulo
    @return modulo importado
    """
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    return importlib.import_module(nombre)


def ejecutar(cmd: list, timeout: float = 15) -> subprocess.CompletedProcess:
    """Ejecuta una orden y captura su salida.

    @param  cmd      orden y argumentos
    @param  timeout  tiempo maximo en segundos
    @return resultado de subprocess.run
    """
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def es_root() -> bool:
    """Indica si las pruebas se ejecutan como superusuario.

    @return True si el usuario efectivo es root
    """
    return os.geteuid() == 0


def activo(unidad: str) -> bool:
    """Indica si una unidad systemd esta activa.

    @param  unidad  nombre de la unidad
    @return True si esta activa
    """
    return ejecutar(["systemctl", "is-active", "--quiet", unidad]).returncode == 0


def propiedad(unidad: str, nombre: str) -> str:
    """Lee una propiedad de una unidad systemd.

    @param  unidad  nombre de la unidad
    @param  nombre  nombre de la propiedad
    @return valor de la propiedad como texto
    """
    salida = ejecutar(["systemctl", "show", unidad, "-p", nombre, "--value"])
    return salida.stdout.strip()


def pid_principal(unidad: str) -> int:
    """Obtiene el PID del proceso principal de una unidad.

    @param  unidad  nombre de la unidad
    @return PID, o 0 si no esta en marcha
    """
    try:
        return int(propiedad(unidad, "MainPID") or 0)
    except ValueError:
        return 0


def unidad_detector() -> str:
    """Identifica la unidad del detector que esta en marcha (real o
    simulado).

    @return nombre de la unidad, o cadena vacia si no hay ninguna
    """
    if activo(UNIDAD_RF):
        return UNIDAD_RF
    if activo(UNIDAD_STUB):
        return UNIDAD_STUB
    return ""


def esperar(condicion, timeout: float, paso: float = 0.5):
    """Espera a que una condicion se cumpla, comprobandola periodicamente.

    @param  condicion  funcion sin argumentos que devuelve True al cumplirse
    @param  timeout    tiempo maximo en segundos
    @param  paso       intervalo entre comprobaciones en segundos
    @return segundos transcurridos, o None si no se cumplio
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if condicion():
            return time.monotonic() - t0
        time.sleep(paso)
    return None


def leer_snapshot() -> dict:
    """Lee el ultimo analisis publicado por el detector.

    @return diccionario del analisis, o vacio si no se puede leer
    """
    try:
        with open(SNAPSHOT) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def edad_snapshot() -> float:
    """Calcula la antiguedad del ultimo analisis del detector.

    @return segundos desde la ultima escritura, o infinito si no existe
    """
    try:
        return time.time() - SNAPSHOT.stat().st_mtime
    except OSError:
        return float("inf")


def esperar_detector_listo(timeout: float = 150) -> str:
    """Espera a que el detector real publique analisis recientes con datos
    del receptor.

    @param  timeout  tiempo maximo en segundos
    @return cadena vacia si esta listo, o el motivo si no lo esta
    """
    def listo():
        """Comprueba si el detector esta activo y publica analisis con datos.

        @return True si esta listo
        """
        s = leer_snapshot()
        return (activo(UNIDAD_RF) and edad_snapshot() < 40
                and s.get("receptor", "ok") != "sin_datos")
    if esperar(listo, timeout, paso=2) is not None:
        return ""
    s = leer_snapshot()
    return (f"el detector no publica análisis con datos del receptor en {timeout:.0f} s "
            f"(servicio activo: {activo(UNIDAD_RF)}; último análisis hace "
            f"{edad_snapshot():.0f} s; receptor: {s.get('receptor', 'sin campo')})")
