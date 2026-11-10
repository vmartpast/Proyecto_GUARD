#!/usr/bin/env python3
"""guard_rf: detector de actividad radioelectrica no identificada. Barre la
banda con el HackRF, separa las emisiones continuas de las de salto de
frecuencia por su persistencia y avisa de las que no estan en la lista blanca.
"""

import argparse
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# Parametros

BANDA_MIN_MHZ = 2400
BANDA_MAX_MHZ = 2500
ANCHO_BIN_HZ = 1_000_000

N_BARRIDOS = 10
DURACION_BARRIDO_S = 1.5

# Margen sobre el suelo de ruido para considerar un bin ocupado en un
# barrido individual.
MARGEN_DB = 8.0

# Umbrales de persistencia, determinados experimentalmente:
#   WiFi con trafico real .... 40-80 %, irregular segun la carga del momento
#   Bluetooth activo ......... 10-12 %, repartido por toda la banda
# Un umbral alto fragmenta el WiFi en bins sueltos; la separacion real
# entre ambas familias esta en torno al 30 %.
UMBRAL_CONTINUA = 0.35
UMBRAL_ESPORADICA = 0.25

# Minimos para descartar ruido impulsivo.
MIN_BINS_CONTINUA = 3
MIN_BINS_FHSS = 6

# Canales WiFi de 2,4 GHz (centro en MHz). Nominalmente 20 MHz de ancho, de
# los que unos 10-14 superan un umbral situado 8-10 dB sobre el ruido: las
# faldas del canal quedan por debajo.
CANALES_WIFI = {
    1: 2412, 2: 2417, 3: 2422, 4: 2427, 5: 2432, 6: 2437,
    7: 2442, 8: 2447, 9: 2452, 10: 2457, 11: 2462, 12: 2467,
    13: 2472, 14: 2484,
}
TOLERANCIA_WIFI_MHZ = 4
ANCHO_WIFI_MIN = 8
ANCHO_WIFI_MAX = 18

HEALTH_FILE = Path("/run/guard/detector.health")
CONFIG_DEF = Path("/opt/guard_device/config/lista_blanca.json")

# Instantanea del ultimo analisis, para consumidores que necesitan mas que
# los eventos de alerta (el panel muestra tambien el suelo de ruido, las
# emisiones conocidas y el perfil completo del espectro).
#
# Vive en /run, que es tmpfs: no desgasta la tarjeta y se limpia sola en
# cada arranque, de modo que nunca se presenta como actual un analisis de
# la sesion anterior.
SNAPSHOT_FILE = Path("/run/guard/spectrum.json")

# Capturas finas pedidas desde el panel. El panel deja la peticion, el
# detector la atiende y deja la respuesta: mismo patron de fichero que el
# snapshot y las pulsaciones, y por la misma razon. El HackRF solo admite
# un dueno, asi que quien graba es el detector, no el panel.
PETICION_FILE = Path("/run/guard/peticion.json")
RESPUESTA_FILE = Path("/run/guard/respuesta.json")
DATASET_DIR = Path("/var/lib/guard/dataset")

# Una captura fina obliga a cerrar hackrf_sweep y volver a abrirlo. Ya se
# vio que abrir y cerrar el receptor en rafaga lo deja atascado, asi que
# se limita la cadencia. Una pausa de vigilancia cada cinco segundos como
# mucho es ademas lo minimo razonable para un equipo que vigila.
CAPTURA_MIN_S = 5.0
CAPTURA_MS_MAX = 500.0

# Capturas para el clasificador de UAV. El detector no clasifica: graba un
# segundo de IQ y lo deja en tmpfs para guard_clasificador, que corre en su
# propio proceso. Asi el calculo de rasgos (CPU) nunca retrasa la
# vigilancia, y un fallo del clasificador no tumba al detector.
#
# Una captura de 1 s pausa la vigilancia unos 2 s. Cada 20 s supone ~10 %
# de ceguera, que se registra en el diario igual que las capturas finas.
CLASIF_DIR = Path("/run/guard/clasif")
CLASIF_PETICION = Path("/run/guard/clasif_peticion.json")
# El clasificador deja aqui la frecuencia de una captura dudosa ("posible
# UAV") para que el detector repita la captura en el barrido siguiente y
# confirme o descarte. Va por delante de cualquier otra eleccion y no
# espera a CLASIF_URGENTE_MIN_S: confirmar rapido es lo que acorta la
# alarma. Si nadie la atiende en CONFIRMAR_CADUCA_S, se ignora.
CLASIF_CONFIRMAR = Path("/run/guard/clasif_confirmar.json")
CONFIRMAR_CADUCA_S = 30.0
CLASIF_CADA_S = 20.0
# 0,5 s = 10 ventanas de 50 ms. Medido sobre los datos de evaluacion:
# decidir por mayoria con 10 ventanas da sensibilidad 90 %, precision 95 %
# y 0 falsas alarmas, igual o mejor que con 20, con la mitad de pausa de
# vigilancia y la mitad de calculo.
CLASIF_SEG = 0.5
# Una emision nueva no identificada dispara una captura sin esperar al
# turno, pero no mas de una cada este tiempo: en un entorno con mucha
# actividad, disparar con cada novedad dejaria la banda sin vigilar.
CLASIF_URGENTE_MIN_S = 8.0

# Histeresis de presentacion. Con la ventana deslizante el analisis se
# recalcula en cada barrido y cada barrido nuevo mueve la persistencia de
# un bin en +-0,1: una emision con persistencia cerca del umbral (0,35)
# aparece y desaparece cada 1,5 s. Una emision se publica en cuanto supera
# el umbral (el aviso sigue siendo inmediato), pero solo se retira cuando
# falta en este numero de analisis seguidos (~12 s).
# Simulado con un canal ocupado el 36 % de los barridos (el peor caso, en
# el umbral): sin retencion, 17 parpadeos en 130 refrescos; con 8, 5. Una
# emision que se apaga tarda en retirarse lo mismo que tardaba el panel en
# refrescarse antes de la ventana deslizante.
RETENCION_ANALISIS = 8
# 20 MHz por captura: cinco centros cubren la banda de 2400 a 2500 MHz.
CLASIF_CENTROS_MHZ = (2410, 2430, 2450, 2470, 2490)
# Si el clasificador no ha recogido la captura anterior en este tiempo, se
# da por perdida y se borra: tmpfs es memoria y no debe llenarse.
CLASIF_CADUCA_S = 60.0

# Solo el modo continuo atiende peticiones: en modo informe o calibracion
# no hay panel esperando ni vigilancia que pausar.
_atender_peticiones = False
_ultima_captura = 0.0
_serie_receptor = None

_clasif_cada = 0.0          # 0 = desactivado (se fija en modo continuo)
_clasif_ganancia = (32, 32)
_clasif_ultima = 0.0
_clasif_turno = 0
_clasif_objetivo = None     # emision nueva que pide captura inmediata
_ultimas_emisiones = []

_running = True


def _stop(signum, frame):
    """Detiene el bucle principal al recibir SIGTERM o SIGINT.

    @param  signum  senal recibida
    @param  frame   marco de pila actual (no se usa)
    """
    global _running
    _running = False


# Utilidades

def now_iso() -> str:
    """Devuelve la fecha y hora actuales en UTC con milisegundos.

    @return marca de tiempo en formato ISO 8601
    """
    ahora = datetime.now(timezone.utc)
    return ahora.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ahora.microsecond // 1000:03d}Z"


def emit(obj: dict) -> None:
    """Escribe un evento JSON por la salida estandar, que recoge el diario
    del sistema.

    @param  obj  evento a emitir
    """
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def notificar_systemd(mensaje: bytes) -> None:
    """Envia un aviso a systemd (protocolo sd_notify) si el servicio lo
    espera; con WatchdogSec, su ausencia provoca el reinicio.

    @param  mensaje  mensaje de estado (READY=1, WATCHDOG=1...)
    """
    destino = os.environ.get("NOTIFY_SOCKET")
    if not destino:
        return
    if destino.startswith("@"):
        destino = "\0" + destino[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(destino)
            s.sendall(mensaje)
    except OSError:
        pass


def touch_health() -> None:
    """Actualiza el fichero de latido que vigila el puente."""
    notificar_systemd(b"WATCHDOG=1")
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.touch()
    except OSError:
        pass


def mediana(valores: list) -> float:
    """Calcula la mediana de una lista de valores.

    @param  valores  lista de valores
    @return mediana
    """
    v = sorted(valores)
    n = len(v)
    if n == 0:
        return 0.0
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def agrupar_contiguas(frecuencias: list) -> list:
    """Agrupa frecuencias contiguas en rangos.

    @param  frecuencias  frecuencias ordenadas en MHz
    @return lista de tuplas (inicio, fin)
    """
    if not frecuencias:
        return []
    grupos = []
    ini = ant = frecuencias[0]
    for f in frecuencias[1:]:
        if f == ant + 1:
            ant = f
        else:
            grupos.append((ini, ant))
            ini = ant = f
    grupos.append((ini, ant))
    return grupos


# Captura

def _cerrar(proc) -> None:
    """Cierra un proceso hackrf_sweep sin dejarlo huerfano.

    @param  proc  proceso a cerrar
    """
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class Receptor:
    """Mantiene un unico proceso hackrf_sweep en marcha y reparte su salida
    en barridos por tiempo.
    """

    # Si no llega una sola linea en este tiempo, el receptor esta
    # atascado: el proceso sigue vivo pero no entrega nada.
    SILENCIO_MAX_S = 8.0

    def __init__(self, f_min: int, f_max: int, ancho_bin: int):
        """Prepara el receptor para la banda indicada, sin arrancarlo.

        @param  f_min      frecuencia inicial en MHz
        @param  f_max      frecuencia final en MHz
        @param  ancho_bin  ancho de bin en Hz
        """
        self.f_min = f_min
        self.f_max = f_max
        self.ancho_bin = ancho_bin
        self._proc = None
        self._hilo = None
        self._acc = {}
        self._lock = threading.Lock()
        self._ultima_linea = 0.0

    # -- ciclo de vida

    def arrancar(self) -> bool:
        """Arranca hackrf_sweep y el hilo que lee su salida.

        @return True si el proceso arranco
        """
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            self._proc = subprocess.Popen(
                ["hackrf_sweep", "-f", f"{self.f_min}:{self.f_max}",
                 "-w", str(self.ancho_bin)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            emit({"ts": now_iso(), "type": "error",
                  "msg": f"no se pudo ejecutar hackrf_sweep: {exc}"})
            self._proc = None
            return False

        with self._lock:
            self._acc = {}
        self._ultima_linea = time.monotonic()
        self._hilo = threading.Thread(target=self._leer, daemon=True)
        self._hilo.start()
        return True

    def parar(self) -> None:
        """Detiene hackrf_sweep y el hilo lector."""
        _cerrar(self._proc)
        if self._hilo is not None:
            self._hilo.join(timeout=3)
        self._proc = None
        self._hilo = None

    def reiniciar(self) -> bool:
        """Para y vuelve a arrancar el receptor cuando deja de entregar datos.

        @return True si volvio a arrancar
        """
        self.parar()
        time.sleep(1.0)
        return self.arrancar()

    # -- lectura

    def _leer(self) -> None:
        """Hilo lector: consume la salida de hackrf_sweep de forma continua y
        acumula el maximo de cada bin.
        """
        try:
            for linea in self._proc.stdout:
                self._ultima_linea = time.monotonic()
                campos = linea.strip().split(", ")
                if len(campos) < 7:
                    continue
                try:
                    f_ini = int(campos[2])
                    paso = float(campos[4])
                    valores = [(int((f_ini + k * paso) / 1e6), float(v))
                               for k, v in enumerate(campos[6:])]
                except (ValueError, IndexError):
                    continue
                with self._lock:
                    for f, db in valores:
                        if f not in self._acc or db > self._acc[f]:
                            self._acc[f] = db
        except (OSError, ValueError):
            pass

    def proceso_vivo(self) -> bool:
        """Indica si el proceso hackrf_sweep existe y no ha terminado.

        @return True si el proceso sigue en marcha
        """
        return self._proc is not None and self._proc.poll() is None

    def vivo(self) -> bool:
        """Indica si el receptor esta en marcha y entregando datos recientes.

        @return True si esta vivo y con datos
        """
        return (self.proceso_vivo()
                and time.monotonic() - self._ultima_linea < self.SILENCIO_MAX_S)

    def tomar_barrido(self, duracion: float) -> dict:
        """Recoge una ventana de medida con el maximo de cada bin durante el
        tiempo indicado.

        @param  duracion  duracion de la ventana en segundos
        @return diccionario frecuencia en MHz -> potencia maxima en dB
        """
        with self._lock:
            self._acc = {}
        fin = time.monotonic() + duracion
        while _running and time.monotonic() < fin:
            # Si el proceso ha muerto —receptor desconectado, USB
            # caido— no tiene sentido agotar la ventana esperando datos
            # que no van a llegar. Salir aqui es lo que separa avisar en
            # dos segundos de avisar en quince.
            if not self.proceso_vivo():
                break
            time.sleep(min(0.1, max(0.0, fin - time.monotonic())))
        with self._lock:
            barrido = self._acc
            self._acc = {}
        return barrido


# Receptor compartido por todo el proceso: el HackRF solo admite un
# dueno, y abrirlo mas de una vez es precisamente lo que se quiere
# evitar.
_receptor = None


def receptor(f_min: int, f_max: int, ancho_bin: int) -> "Receptor":
    """Devuelve el receptor compartido del proceso, creandolo si no existe.

    @param  f_min      frecuencia inicial en MHz
    @param  f_max      frecuencia final en MHz
    @param  ancho_bin  ancho de bin en Hz
    @return objeto Receptor
    """
    global _receptor
    if (_receptor is None or _receptor.f_min != f_min
            or _receptor.f_max != f_max or _receptor.ancho_bin != ancho_bin):
        if _receptor is not None:
            _receptor.parar()
        _receptor = Receptor(f_min, f_max, ancho_bin)
    return _receptor


def cerrar_receptor() -> None:
    """Detiene el receptor compartido, si existe."""
    global _receptor
    if _receptor is not None:
        _receptor.parar()
        _receptor = None


def un_barrido(f_min: int, f_max: int, ancho_bin: int,
               duracion: float) -> dict:
    """Realiza un barrido suelto de la banda.

    @param  f_min      frecuencia inicial en MHz
    @param  f_max      frecuencia final en MHz
    @param  ancho_bin  ancho de bin en Hz
    @param  duracion   duracion del barrido en segundos
    @return diccionario frecuencia en MHz -> potencia maxima en dB
    """
    rx = receptor(f_min, f_max, ancho_bin)
    if not rx.arrancar():
        return {}
    return rx.tomar_barrido(duracion)


def _responder(pid, estado: str, **campos) -> None:
    """Escribe de forma atomica la respuesta a una peticion del panel.

    @param  pid       identificador de la peticion
    @param  estado    estado de la respuesta
    @param  **campos  campos adicionales de la respuesta
    """
    datos = {"id": pid, "estado": estado, "ts": now_iso(), **campos}
    try:
        tmp = RESPUESTA_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(datos, f, separators=(",", ":"))
        os.replace(tmp, RESPUESTA_FILE)
    except OSError as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"no se pudo responder a la peticion: {exc}"})


def atender_peticion(rx: "Receptor") -> None:
    """Realiza la captura fina pedida desde el panel, si hay alguna. Se
    llama entre barridos.

    @param  rx  receptor compartido
    """
    global _ultima_captura, _serie_receptor

    if not _atender_peticiones or not PETICION_FILE.is_file():
        return

    try:
        pet = json.loads(PETICION_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        pet = None
    # Se consume siempre, valida o no: una peticion corrupta que se
    # quedara en disco se volveria a leer en cada barrido.
    try:
        PETICION_FILE.unlink()
    except OSError:
        pass

    if not pet or pet.get("accion") != "captura_fina":
        return
    pid = pet.get("id")

    espera = CAPTURA_MIN_S - (time.monotonic() - _ultima_captura)
    if espera > 0:
        _responder(pid, "error",
                   msg=f"demasiado seguido: espera {espera:.0f} s")
        return

    try:
        f_mhz = float(pet["f_centro_mhz"])
        ms = max(5.0, min(CAPTURA_MS_MAX, float(pet.get("ms", 50))))
    except (KeyError, TypeError, ValueError):
        _responder(pid, "error", msg="peticion mal formada")
        return

    emit({"ts": now_iso(), "type": "status", "mode": "rf",
          "vigilancia": "pausada", "motivo": "captura_fina",
          "f_centro_mhz": f_mhz, "ms": ms})
    t0 = time.monotonic()
    rx.parar()
    # Margen para que libusb suelte el dispositivo antes de reabrirlo con
    # otro proceso; sin el, hackrf_transfer puede encontrarlo ocupado.
    time.sleep(0.3)

    try:
        # guard_captura es quien define el formato del dataset. Se reusa
        # en lugar de duplicarlo: una captura hecha desde el panel tiene
        # que ser indistinguible de una hecha a mano.
        import guard_captura as gc
        if _serie_receptor is None:
            _serie_receptor = gc.serial_hackrf()
        ns = SimpleNamespace(
            etiqueta=pet.get("etiqueta", "sin_etiquetar"),
            nota=pet.get("nota", "capturado desde el panel"),
            f=f_mhz, muestreo=gc.MUESTREO_SPS, duracion=ms / 1000.0,
            lna=gc.LNA_DB, vga=gc.VGA_DB, amp=gc.AMP,
            n=1, sin_hash=True)
        # una_captura informa por stdout, que aqui es el canal de eventos
        # del detector hacia el diario. Se desvia para no mezclarlo.
        with contextlib.redirect_stdout(io.StringIO()):
            reg = gc.una_captura(ns, DATASET_DIR, _serie_receptor, 1)
        _responder(pid, "ok", fichero=str(DATASET_DIR / reg["fichero"]),
                   f_centro_mhz=f_mhz, ms=ms)
    except Exception as exc:          # noqa: BLE001 — se informa al panel
        _responder(pid, "error", msg=str(exc)[:300])
    finally:
        _ultima_captura = time.monotonic()
        time.sleep(0.3)
        rx.arrancar()
        emit({"ts": now_iso(), "type": "status", "mode": "rf",
              "vigilancia": "reanudada",
              "pausa_s": round(time.monotonic() - t0, 2)})


def _escribir_atomico(ruta: Path, datos: dict) -> None:
    """Escribe un JSON de forma atomica (temporal y renombrado).

    @param  ruta   fichero de destino
    @param  datos  contenido a escribir
    """
    tmp = ruta.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(datos, f, separators=(",", ":"))
    os.replace(tmp, ruta)


def leer_confirmacion():
    """Lee la peticion de confirmacion del clasificador.

    @return frecuencia en MHz a confirmar, o None si no hay ninguna vigente
    """
    try:
        if time.time() - CLASIF_CONFIRMAR.stat().st_mtime > CONFIRMAR_CADUCA_S:
            CLASIF_CONFIRMAR.unlink()
            return None
        return float(json.loads(CLASIF_CONFIRMAR.read_text())["f_centro_mhz"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def elegir_centro() -> tuple:
    """Decide la frecuencia de la siguiente captura para el clasificador:
    confirmacion, emision nueva, emision conocida o turno rotatorio.

    @return tupla (frecuencia en MHz, motivo)
    """
    global _clasif_turno
    f = leer_confirmacion()
    if f is not None:
        return f, "confirmacion"
    if _clasif_objetivo is not None:
        f = float(min(max(float(_clasif_objetivo["f_centro_mhz"]),
                          CLASIF_CENTROS_MHZ[0]), CLASIF_CENTROS_MHZ[-1]))
        return f, "nueva"
    sospechosas = [e for e in _ultimas_emisiones if not e.get("conocido")]
    if sospechosas:
        e = max(sospechosas, key=lambda x: x.get("snr_db", 0))
        f = float(min(max(float(e["f_centro_mhz"]), CLASIF_CENTROS_MHZ[0]),
                      CLASIF_CENTROS_MHZ[-1]))
        return f, "emision"
    f = CLASIF_CENTROS_MHZ[_clasif_turno % len(CLASIF_CENTROS_MHZ)]
    _clasif_turno += 1
    return float(f), "turno"


def limpiar_clasif() -> None:
    """Borra las capturas y peticiones de clasificacion pendientes."""
    for f in list(CLASIF_DIR.rglob("*")) if CLASIF_DIR.exists() else []:
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass
    for f in (CLASIF_PETICION, CLASIF_CONFIRMAR):
        try:
            f.unlink()
        except OSError:
            pass


def capturar_para_clasificar(rx: "Receptor") -> None:
    """Graba 0,5 s de IQ para el clasificador, si toca y si ya recogio la
    anterior. Se llama entre barridos.

    @param  rx  receptor compartido
    """
    global _clasif_ultima, _serie_receptor, _clasif_objetivo

    if not _clasif_cada:
        return
    desde_ultima = time.monotonic() - _clasif_ultima
    urgente = ((_clasif_objetivo is not None
                and desde_ultima >= CLASIF_URGENTE_MIN_S)
               or leer_confirmacion() is not None)
    if not urgente and desde_ultima < _clasif_cada:
        return
    # La pausa por captura fina cuenta: no se encadenan dos pausas seguidas.
    if time.monotonic() - _ultima_captura < CAPTURA_MIN_S:
        return

    if CLASIF_PETICION.exists():
        edad = time.time() - CLASIF_PETICION.stat().st_mtime
        if edad < CLASIF_CADUCA_S:
            return                       # el clasificador va retrasado
        emit({"ts": now_iso(), "type": "error", "mode": "rf",
              "subsistema": "clasificador",
              "msg": f"captura sin recoger durante {edad:.0f} s, se descarta"})
        limpiar_clasif()

    f_mhz, motivo = elegir_centro()
    if motivo == "confirmacion":
        try:
            CLASIF_CONFIRMAR.unlink()
        except OSError:
            pass
    lna, vga = _clasif_ganancia
    emit({"ts": now_iso(), "type": "status", "mode": "rf",
          "vigilancia": "pausada", "motivo": "clasificacion",
          "f_centro_mhz": f_mhz, "segundos": CLASIF_SEG, "eleccion": motivo})
    t0 = time.monotonic()
    rx.parar()
    time.sleep(0.3)
    try:
        import guard_captura as gc
        if _serie_receptor is None:
            _serie_receptor = gc.serial_hackrf()
        CLASIF_DIR.mkdir(parents=True, exist_ok=True)
        ns = SimpleNamespace(
            etiqueta="clasif", nota=f"para el clasificador ({motivo})",
            f=f_mhz, muestreo=gc.MUESTREO_SPS, duracion=CLASIF_SEG,
            lna=lna, vga=vga, amp=gc.AMP, n=1, sin_hash=True)
        with contextlib.redirect_stdout(io.StringIO()):
            reg = gc.una_captura(ns, CLASIF_DIR, _serie_receptor, 1)
        # El manifiesto solo tiene sentido en un dataset; aqui creceria
        # sin fin en memoria.
        try:
            (CLASIF_DIR / "manifest.jsonl").unlink()
        except OSError:
            pass
        _escribir_atomico(CLASIF_PETICION, {
            "id": int(time.time() * 1000),
            "fichero": str(CLASIF_DIR / reg["fichero"]),
            "f_centro_mhz": f_mhz, "motivo": motivo,
            "ganancia_lna_db": lna, "ganancia_vga_db": vga,
            "ts": now_iso()})
    except Exception as exc:          # noqa: BLE001 — se registra y se sigue
        emit({"ts": now_iso(), "type": "error", "mode": "rf",
              "subsistema": "clasificador",
              "msg": f"captura para clasificar fallida: {str(exc)[:200]}"})
    finally:
        _clasif_ultima = time.monotonic()
        _clasif_objetivo = None
        time.sleep(0.3)
        rx.arrancar()
        emit({"ts": now_iso(), "type": "status", "mode": "rf",
              "vigilancia": "reanudada", "motivo": "clasificacion",
              "pausa_s": round(time.monotonic() - t0, 2)})


def capturar(args, progreso: bool = False) -> list:
    """Toma N barridos consecutivos del flujo del receptor.

    @param  args      argumentos de la linea de ordenes
    @param  progreso  mostrar el progreso por pantalla
    @return lista de barridos, o vacia si el receptor falla
    """
    rx = receptor(args.f_min, args.f_max, args.ancho_bin)

    if not rx.arrancar():
        return []

    # Un receptor que sigue vivo pero lleva segundos sin entregar una
    # linea esta atascado. Se reintenta una vez antes de darlo por
    # perdido; si tampoco asi responde, se devuelve vacio y el bucle
    # principal lo declara caido en el snapshot y en el diario.
    if not rx.vivo():
        emit({"ts": now_iso(), "type": "error", "mode": "rf",
              "subsistema": "receptor", "estado": "reiniciando",
              "msg": "sin datos del receptor, reabriendo hackrf_sweep"})
        if not rx.reiniciar():
            return []
        time.sleep(1.0)
        if not rx.vivo():
            return []

    barridos = []
    for i in range(args.barridos):
        if not _running:
            break
        if progreso:
            print(f"\r  barrido {i + 1}/{args.barridos}...", end="", flush=True)
        atender_peticion(rx)
        capturar_para_clasificar(rx)
        barridos.append(rx.tomar_barrido(args.duracion))
        # Igual que dentro de la ventana: si el receptor ha desaparecido,
        # encadenar los barridos que faltan solo retrasa el aviso.
        if not rx.proceso_vivo():
            break
    if progreso:
        print("\r" + " " * 40 + "\r", end="")
    return barridos


# Analisis

def analizar(barridos: list, margen: float) -> dict:
    """Calcula en un solo pase el suelo de ruido, la persistencia y la
    energia de cada bin.

    @param  barridos  lista de barridos
    @param  margen    margen sobre el suelo de ruido en dB
    @return diccionario con el analisis
    """
    validos = [b for b in barridos if b]
    n = len(validos)
    if n == 0:
        return {"n_barridos": 0, "suelo_ruido_db": 0.0, "umbral_db": 0.0,
                "persistencia": {}, "picos": {}}

    ocupaciones = {}
    picos = {}
    suelos = []

    for potencias in validos:
        suelo = mediana(list(potencias.values()))
        suelos.append(suelo)
        umbral = suelo + margen
        for f, db in potencias.items():
            if db >= umbral:
                ocupaciones[f] = ocupaciones.get(f, 0) + 1
                if f not in picos or db > picos[f]:
                    picos[f] = db

    persistencia = {f: c / n for f, c in ocupaciones.items()}
    suelo_global = mediana(suelos)

    return {
        "n_barridos": n,
        "suelo_ruido_db": round(suelo_global, 1),
        "umbral_db": round(suelo_global + margen, 1),
        "persistencia": persistencia,
        "picos": picos,
    }


def clasificar_continua(ini: int, fin: int) -> tuple:
    """Clasifica una emision continua por su ancho y su frecuencia central.

    @param  ini  frecuencia inicial en MHz
    @param  fin  frecuencia final en MHz
    @return tupla (tipo, detalle)
    """
    ancho = fin - ini + 1
    centro = (ini + fin) / 2

    if ANCHO_WIFI_MIN <= ancho <= ANCHO_WIFI_MAX:
        candidatos = [(abs(centro - f), c) for c, f in CANALES_WIFI.items()
                      if abs(centro - f) <= TOLERANCIA_WIFI_MHZ]
        if candidatos:
            _, canal = min(candidatos)
            return "wifi", f"canal {canal}"
        return "banda_media", "ancho compatible con WiFi, centro no normalizado"

    if ancho >= 25:
        return "banda_ancha", "ocupacion amplia en frecuencia fija"

    return "continua", "emision en frecuencia fija"


def identificar(analisis: dict) -> list:
    """Convierte el analisis de persistencia y energia en emisiones
    continuas o de salto de frecuencia.

    @param  analisis  resultado de analizar
    @return lista de emisiones
    """
    persistencia = analisis["persistencia"]
    picos = analisis["picos"]
    suelo = analisis["suelo_ruido_db"]
    emisiones = []

    # --- emisiones en frecuencia fija
    # Se agrupan por contiguidad antes de clasificar: un bloque de bins
    # adyacentes con persistencia media es una unica emision, no varias de
    # 1 MHz. Se incluyen los bins intermedios porque una emision con
    # trafico irregular no alcanza persistencia alta en todos sus bins.
    fijas = sorted(f for f, p in persistencia.items() if p > UMBRAL_ESPORADICA)

    for ini, fin in agrupar_contiguas(fijas):
        bins = list(range(ini, fin + 1))
        if len(bins) < MIN_BINS_CONTINUA:
            continue
        p_media = sum(persistencia.get(f, 0.0) for f in bins) / len(bins)
        if p_media < UMBRAL_CONTINUA:
            continue

        pico = max(picos.get(f, suelo) for f in bins)
        tipo, detalle = clasificar_continua(ini, fin)
        emisiones.append({
            "clase": "continua",
            "tipo": tipo,
            "detalle": detalle,
            "f_inicio_mhz": ini,
            "f_fin_mhz": fin,
            "f_centro_mhz": (ini + fin) / 2,
            "ancho_mhz": len(bins),
            "persistencia": round(p_media, 2),
            "pico_db": round(pico, 1),
            "snr_db": round(pico - suelo, 1),
        })

    # --- emisiones de salto de frecuencia
    asignados = set()
    for e in emisiones:
        asignados.update(range(e["f_inicio_mhz"], e["f_fin_mhz"] + 1))

    esporadicas = sorted(f for f, p in persistencia.items()
                         if p <= UMBRAL_ESPORADICA and f not in asignados)

    if len(esporadicas) >= MIN_BINS_FHSS:
        dispersion = max(esporadicas) - min(esporadicas)
        pico = max(picos.get(f, suelo) for f in esporadicas)
        p_media = sum(persistencia[f] for f in esporadicas) / len(esporadicas)
        emisiones.append({
            "clase": "salto_frecuencia",
            "tipo": "fhss",
            "detalle": f"{len(esporadicas)} canales sobre {dispersion} MHz",
            "f_inicio_mhz": min(esporadicas),
            "f_fin_mhz": max(esporadicas),
            "f_centro_mhz": (min(esporadicas) + max(esporadicas)) / 2,
            "ancho_mhz": dispersion,
            "canales_visitados": len(esporadicas),
            "persistencia": round(p_media, 2),
            "pico_db": round(pico, 1),
            "snr_db": round(pico - suelo, 1),
        })

    return emisiones


# Lista blanca

def cargar_lista_blanca(ruta: Path) -> list:
    """Lee la lista blanca de emisores conocidos.

    @param  ruta  fichero JSON de la lista blanca
    @return lista de emisores, o vacia si no existe o no es valido
    """
    if not ruta.is_file():
        return []
    try:
        with open(ruta) as f:
            return json.load(f).get("emisores", [])
    except (OSError, json.JSONDecodeError) as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"lista blanca ilegible: {exc}"})
        return []


def marcar_conocidos(emisiones: list, lista: list) -> None:
    """Anota en cada emision su etiqueta de la lista blanca, si la tiene.

    @param  emisiones  lista de emisiones
    @param  lista      lista de emisores conocidos
    """
    for e in emisiones:
        e["conocido"] = False
        e["etiqueta"] = None
        for entrada in lista:
            f_ini = entrada.get("f_inicio_mhz", 0)
            f_fin = entrada.get("f_fin_mhz", 0)
            # Una entrada de la lista solo silencia emisiones de su misma
            # clase: un WiFi declarado no debe enmascarar una FHSS que
            # comparta banda, que es justamente lo que interesa detectar.
            #
            # Una entrada SIN clase se trata como continua, no como
            # comodin. Antes valia para cualquier clase, y una lista
            # generada por una version anterior del calibrador —sin ese
            # campo— acabo ocultando un Bluetooth bajo la entrada del WiFi
            # propio. Lo que no debe ocultarse nunca por omision es una
            # emision de salto de frecuencia, que es la familia a la que
            # pertenece un enlace de control de UAV.
            clase = entrada.get("clase", "continua")
            if clase != e["clase"]:
                continue
            if e["f_fin_mhz"] >= f_ini and e["f_inicio_mhz"] <= f_fin:
                e["conocido"] = True
                e["etiqueta"] = entrada.get("etiqueta", "sin etiqueta")
                break


# Salidas

def imprimir_cabecera(args, lista: list) -> None:
    """Muestra la configuracion del analisis por pantalla.

    @param  args   argumentos de la linea de ordenes
    @param  lista  lista de emisores conocidos
    """
    print("=" * 74)
    print("  GUARD - detector de actividad radioelectrica")
    print("=" * 74)
    print(f"\n  banda        : {args.f_min}-{args.f_max} MHz "
          f"({args.ancho_bin / 1e6:.0f} MHz por bin)")
    print(f"  muestreo     : {args.barridos} barridos x {args.duracion:.1f} s "
          f"(~{args.barridos * args.duracion:.0f} s)")
    print(f"  lista blanca : {len(lista)} emisor(es) declarado(s)")


def imprimir_mapa(analisis: dict, f_min: int, f_max: int) -> None:
    """Muestra en texto el mapa de persistencia por frecuencia.

    @param  analisis  resultado de analizar
    @param  f_min     frecuencia inicial en MHz
    @param  f_max     frecuencia final en MHz
    """
    persistencia = analisis["persistencia"]
    print("\n  Persistencia por frecuencia\n")
    print("       0%                                              100%")
    print("       |------------------------------------------------|")
    for f in range(f_min, f_max):
        p = persistencia.get(f, 0.0)
        if p == 0:
            continue
        barra = int(p * 48)
        if p >= UMBRAL_CONTINUA:
            marca = "="
        elif p <= UMBRAL_ESPORADICA:
            marca = "."
        else:
            marca = "-"
        print(f"  {f}  {marca * barra}{' ' * (48 - barra)}| {p * 100:3.0f}%")
    print("\n  leyenda:  = continua    - intermedia    . esporadica")


def imprimir_informe(analisis: dict, emisiones: list) -> None:
    """Muestra en texto las emisiones identificadas.

    @param  analisis   resultado de analizar
    @param  emisiones  lista de emisiones
    """
    print(f"\n  suelo de ruido : {analisis['suelo_ruido_db']} dB")
    print(f"  umbral         : {analisis['umbral_db']} dB")

    if not emisiones:
        print("\n  Sin actividad por encima del umbral.")
        return

    desconocidas = [e for e in emisiones if not e.get("conocido")]
    print(f"\n  {len(emisiones)} emision(es), "
          f"{len(desconocidas)} sin identificar\n")

    for e in emisiones:
        banda = f"{e['f_inicio_mhz']}-{e['f_fin_mhz']} MHz"
        if e["clase"] == "continua":
            titulo = f"CONTINUA   {banda} ({e['ancho_mhz']} MHz)"
        else:
            titulo = f"FHSS       {banda} ({e['canales_visitados']} canales)"

        print(f"  {titulo}")
        print(f"             tipo         : {e['tipo']} - {e['detalle']}")
        print(f"             pico / SNR   : {e['pico_db']} dB / {e['snr_db']} dB")
        print(f"             persistencia : {e['persistencia'] * 100:.0f} %")
        if e.get("conocido"):
            print(f"             estado       : CONOCIDA ({e['etiqueta']})")
        else:
            print("             estado       : >>> NO IDENTIFICADA")
        print()


def emitir_eventos(emisiones: list) -> int:
    """Emite un evento por cada emision no identificada.

    @param  emisiones  lista de emisiones
    @return numero de eventos emitidos
    """
    n = 0
    for e in emisiones:
        if e.get("conocido"):
            continue
        emit({
            "ts": now_iso(),
            "type": "detection",
            "confirmed": True,
            "label": "rf_no_identificada",
            "model": None,
            "confidence": min(0.99, round(e["snr_db"] / 30.0, 2)),
            "window_s": 0.0,
            "rssi_dbfs": e["pico_db"],
            "f_centro_mhz": e["f_centro_mhz"],
            "ancho_mhz": e["ancho_mhz"],
            "clase": e["clase"],
            "tipo": e["tipo"],
            "persistencia": e["persistencia"],
        })
        n += 1
    return n


def escribir_snapshot(args, analisis: dict, emisiones: list,
                      receptor: str = "ok", ultimo: dict | None = None,
                      seq: int = 0) -> None:
    """Publica de forma atomica el ultimo analisis para el panel y el
    puente.

    @param  args       argumentos de la linea de ordenes
    @param  analisis   resultado de analizar
    @param  emisiones  lista de emisiones
    @param  receptor   estado del receptor
    @param  ultimo     ultimo barrido completo
    @param  seq        numero de analisis
    """
    try:
        SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({
                "ts": now_iso(),
                "ts_mono": round(time.monotonic(), 1),
                "receptor": receptor,
                "banda_mhz": [args.f_min, args.f_max],
                "barridos": analisis["n_barridos"],
                "duracion_barrido_s": args.duracion,
                "margen_db": args.margen,
                "suelo_ruido_db": analisis["suelo_ruido_db"],
                "umbral_db": analisis["umbral_db"],
                "emisiones": emisiones,
                "persistencia_por_bin": {
                    str(f): round(p, 3)
                    for f, p in sorted(analisis["persistencia"].items())
                },
                "picos_por_bin": {
                    str(f): round(v, 1)
                    for f, v in sorted(analisis["picos"].items())
                },
                # Ultimo barrido completo (todos los bins, no solo los que
                # superan el umbral) y su numero de orden. Con la ventana
                # deslizante, picos_por_bin es el maximo de los ultimos N
                # barridos: sirve para detectar, pero pintado como cascada
                # estira una rafaga de 1,5 s a 15 s. La cascada del panel se
                # construye con esto, una fila por barrido.
                "seq": seq,
                "ultimo_barrido": ({str(f): round(v, 1)
                                    for f, v in sorted(ultimo.items())}
                                   if ultimo else {}),
                "suelo_ultimo_db": (round(mediana(list(ultimo.values())), 1)
                                    if ultimo else None),
            }, f, separators=(",", ":"))
        os.replace(tmp, SNAPSHOT_FILE)
    except OSError as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"no se pudo publicar el snapshot: {exc}"})


def volcar_json(destino: Path, args, analisis: dict, emisiones: list) -> None:
    """Guarda el analisis y las emisiones en un fichero JSON.

    @param  destino    fichero de salida
    @param  args       argumentos de la linea de ordenes
    @param  analisis   resultado de analizar
    @param  emisiones  lista de emisiones
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w") as f:
        json.dump({
            "ts": now_iso(),
            "banda_mhz": [args.f_min, args.f_max],
            "barridos": analisis["n_barridos"],
            "duracion_barrido_s": args.duracion,
            "margen_db": args.margen,
            "suelo_ruido_db": analisis["suelo_ruido_db"],
            "umbral_db": analisis["umbral_db"],
            "emisiones": emisiones,
            "persistencia_por_bin": {
                str(f): round(p, 3)
                for f, p in sorted(analisis["persistencia"].items())
            },
        }, f, indent=2, ensure_ascii=False)


# Modos

def modo_informe(args) -> int:
    """Realiza un analisis puntual y muestra el informe.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    lista = cargar_lista_blanca(args.config)
    if not args.json:
        imprimir_cabecera(args, lista)

    barridos = capturar(args, progreso=not args.json)
    if not any(barridos):
        print("ERROR: ningun barrido devolvio datos.", file=sys.stderr)
        return 1

    analisis = analizar(barridos, args.margen)
    emisiones = identificar(analisis)
    marcar_conocidos(emisiones, lista)

    if args.json:
        print(json.dumps({
            "ts": now_iso(),
            "banda_mhz": [args.f_min, args.f_max],
            "suelo_ruido_db": analisis["suelo_ruido_db"],
            "emisiones": emisiones,
        }, indent=2, ensure_ascii=False))
    else:
        if args.mapa:
            imprimir_mapa(analisis, args.f_min, args.f_max)
        imprimir_informe(analisis, emisiones)

    if args.guardar:
        volcar_json(args.guardar, args, analisis, emisiones)
        if not args.json:
            print(f"  resultado en {args.guardar}")

    return 0


def modo_calibrar(args) -> int:
    """Caracteriza el entorno y propone una lista blanca inicial.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    print("=" * 74)
    print("  GUARD - calibracion del entorno radioelectrico")
    print("=" * 74)
    print(f"\n  Caracterizando {args.f_min}-{args.f_max} MHz...")
    print("  (asegurate de que NO hay emisores de interes activos)\n")

    barridos = capturar(args, progreso=True)
    if not any(barridos):
        print("ERROR: ningun barrido devolvio datos.", file=sys.stderr)
        return 1

    analisis = analizar(barridos, args.margen)
    emisiones = identificar(analisis)
    marcar_conocidos(emisiones, [])
    imprimir_informe(analisis, emisiones)

    propuesta = {
        "generada": now_iso(),
        "nota": ("Revisar antes de usar. Cada entrada silencia esa banda "
                 "para emisiones de la misma clase."),
        "emisores": [
            {
                "etiqueta": f"{e['tipo']} {e['detalle']}".strip(),
                "clase": e["clase"],
                "f_inicio_mhz": e["f_inicio_mhz"],
                "f_fin_mhz": e["f_fin_mhz"],
            }
            for e in emisiones
        ],
    }

    args.config.parent.mkdir(parents=True, exist_ok=True)
    destino = args.config.with_suffix(".propuesta.json")
    with open(destino, "w") as f:
        json.dump(propuesta, f, indent=2, ensure_ascii=False)

    print(f"  Propuesta de lista blanca en:\n    {destino}\n")
    print(f"  Revisala y, si procede:\n    mv {destino} {args.config}")
    return 0


def es_nueva(e: dict, previas: list, margen_mhz: float = 2.0) -> bool:
    """Indica si una emision no se solapa con ninguna del analisis
    anterior.

    @param  e           emision a comprobar
    @param  previas     emisiones del analisis anterior
    @param  margen_mhz  margen de solapamiento en MHz
    @return True si la emision es nueva
    """
    ini, fin = float(e["f_inicio_mhz"]), float(e["f_fin_mhz"])
    return not any(ini <= f1 + margen_mhz and fin >= f0 - margen_mhz
                   for f0, f1 in previas)


def aplicar_retencion(actuales: list, retenidas: list) -> tuple:
    """Mantiene durante unos analisis las emisiones que acaban de faltar,
    para que no desaparezcan y reaparezcan.

    @param  actuales   emisiones del analisis actual
    @param  retenidas  emisiones retenidas de analisis anteriores
    @return tupla (emisiones a publicar, nuevas retenidas)
    """
    def solapan(a, b):
        """Indica si dos emisiones de la misma clase se solapan en frecuencia.

        @param  a  primera emision
        @param  b  segunda emision
        @return True si se solapan
        """
        return (a.get("clase") == b.get("clase")
                and float(a["f_inicio_mhz"]) <= float(b["f_fin_mhz"]) + 2
                and float(a["f_fin_mhz"]) >= float(b["f_inicio_mhz"]) - 2)

    publicar = list(actuales)
    nuevas_retenidas = [(e, 0) for e in actuales]
    for e, ausencias in retenidas:
        if any(solapan(e, a) for a in actuales):
            continue                       # sigue presente: ya esta
        if ausencias + 1 < RETENCION_ANALISIS:
            copia = dict(e, retenida=ausencias + 1)
            publicar.append(copia)
            nuevas_retenidas.append((e, ausencias + 1))
    return publicar, nuevas_retenidas


def tomar_un_barrido(args) -> dict:
    """Toma un barrido del flujo continuo tras atender las capturas
    pendientes, y reinicia el receptor si no responde.

    @param  args  argumentos de la linea de ordenes
    @return barrido, o vacio si el receptor no entrega datos
    """
    rx = receptor(args.f_min, args.f_max, args.ancho_bin)
    if not rx.arrancar():
        return {}
    if not rx.vivo():
        emit({"ts": now_iso(), "type": "error", "mode": "rf",
              "subsistema": "receptor", "estado": "reiniciando",
              "msg": "sin datos del receptor, reabriendo hackrf_sweep"})
        if not rx.reiniciar():
            return {}
        time.sleep(1.0)
        if not rx.vivo():
            return {}
    atender_peticion(rx)
    capturar_para_clasificar(rx)
    return rx.tomar_barrido(args.duracion)


def modo_continuo(args) -> int:
    """Modo servicio: ventana deslizante de barridos, analisis, eventos y
    publicacion del estado.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    global _atender_peticiones, _clasif_cada, _clasif_ganancia
    global _ultimas_emisiones, _clasif_objetivo
    notificar_systemd(b"READY=1")
    _atender_peticiones = True
    _clasif_cada = max(0.0, args.clasificar_cada)
    _clasif_ganancia = (args.clasif_lna, args.clasif_vga)
    # Una peticion que quedo de una sesion anterior no se atiende: nadie
    # la esta esperando ya.
    for viejo in (PETICION_FILE, RESPUESTA_FILE):
        try:
            viejo.unlink()
        except OSError:
            pass
    limpiar_clasif()

    lista = cargar_lista_blanca(args.config)
    emit({"ts": now_iso(), "type": "status", "mode": "rf",
          "pid": os.getpid(), "banda_mhz": [args.f_min, args.f_max],
          "lista_blanca": len(lista)})

    ultimo_status = time.monotonic()
    ciclos_sin_datos = 0

    # Ventana deslizante: se conservan los ultimos N barridos y se analiza
    # despues de CADA barrido, no cada N. La estadistica es la misma (la
    # persistencia sigue midiendose sobre N barridos, con los umbrales de
    # siempre), pero el analisis se refresca cada ~1,5 s en lugar de cada
    # ~17 s: una emision nueva aparece en cuanto acumula persistencia.
    ventana = deque(maxlen=args.barridos)
    bandas_previas = []
    retenidas = []              # [(emision, analisis seguidos ausente)]
    seq = 0                     # numero de barrido, para el panel
    ultimo_evento = 0.0
    periodo_eventos = args.barridos * args.duracion

    while _running:
        touch_health()
        barrido = tomar_un_barrido(args)

        if not barrido:
            # El receptor no entrega datos. Puede ser el USB atascado, el
            # dispositivo retirado o hackrf_sweep fallando al abrirlo. No
            # se puede distinguir desde aqui, pero si se puede —y se
            # debe— decir que no se esta vigilando.
            ventana.clear()
            ciclos_sin_datos += 1
            escribir_snapshot(args, analizar([], args.margen), [],
                              receptor="sin_datos")
            # Se avisa al entrar en el fallo y luego con cuentagotas: un
            # receptor caido no debe inundar el diario, pero tampoco
            # puede desaparecer de el mientras dure.
            if ciclos_sin_datos == 1 or ciclos_sin_datos % 20 == 0:
                emit({"ts": now_iso(), "type": "error", "mode": "rf",
                      "subsistema": "receptor", "estado": "sin_datos",
                      "ciclos": ciclos_sin_datos,
                      "msg": "el receptor no entrega barridos"})
            if _running:
                time.sleep(max(1.0, args.intervalo))
            continue

        if ciclos_sin_datos:
            emit({"ts": now_iso(), "type": "status", "mode": "rf",
                  "subsistema": "receptor", "estado": "recuperado",
                  "ciclos_perdidos": ciclos_sin_datos})
            ciclos_sin_datos = 0

        ventana.append(barrido)
        seq += 1
        if len(ventana) < args.barridos:
            continue                    # llenando la ventana al arrancar

        analisis = analizar(list(ventana), args.margen)
        emisiones = identificar(analisis)
        marcar_conocidos(emisiones, lista)
        emisiones, retenidas = aplicar_retencion(emisiones, retenidas)
        _ultimas_emisiones = emisiones

        # Emisiones no identificadas que no estaban en el analisis anterior
        desconocidas = [e for e in emisiones if not e.get("conocido")]
        nuevas = [e for e in desconocidas if es_nueva(e, bandas_previas)]
        bandas_previas = [(float(e["f_inicio_mhz"]), float(e["f_fin_mhz"]))
                          for e in desconocidas]
        if nuevas:
            # Captura inmediata para el clasificador, centrada en la mas
            # fuerte de las nuevas (respetando CLASIF_URGENTE_MIN_S).
            _clasif_objetivo = max(nuevas, key=lambda e: e.get("snr_db", 0))

        # Dos consumidores, un solo analisis: los eventos van al puente y
        # de ahi a la interfaz fisica; el snapshot lo lee el panel. Los
        # eventos salen al momento si hay algo nuevo y, si no, con la
        # cadencia de antes: analizar cada barrido no debe multiplicar por
        # diez el volumen del diario.
        if nuevas or time.monotonic() - ultimo_evento >= periodo_eventos:
            emitir_eventos(emisiones)
            ultimo_evento = time.monotonic()
        escribir_snapshot(args, analisis, emisiones, ultimo=barrido, seq=seq)

        if time.monotonic() - ultimo_status >= 30:
            emit({"ts": now_iso(), "type": "status", "mode": "rf",
                  "suelo_ruido_db": analisis["suelo_ruido_db"],
                  "emisiones": len(emisiones),
                  "no_identificadas": len(desconocidas)})
            ultimo_status = time.monotonic()

        if _running and args.intervalo > 0:
            time.sleep(args.intervalo)

    emit({"ts": now_iso(), "type": "status", "mode": "rf", "stopping": True})
    return 0


def main() -> int:
    """Lee los argumentos y ejecuta el modo elegido.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(
        description="Detector de actividad radioelectrica no identificada")
    modo = p.add_mutually_exclusive_group()
    modo.add_argument("--continuo", action="store_true",
                      help="servicio: analiza en bucle y emite eventos")
    modo.add_argument("--calibrar", action="store_true",
                      help="caracteriza el entorno y propone lista blanca")

    p.add_argument("--f-min", type=int, default=BANDA_MIN_MHZ)
    p.add_argument("--f-max", type=int, default=BANDA_MAX_MHZ)
    p.add_argument("--ancho-bin", type=int, default=ANCHO_BIN_HZ)
    p.add_argument("--barridos", type=int, default=N_BARRIDOS,
                   help=f"barridos por analisis (def. {N_BARRIDOS})")
    p.add_argument("--duracion", type=float, default=DURACION_BARRIDO_S,
                   help="segundos por barrido individual")
    p.add_argument("--margen", type=float, default=MARGEN_DB,
                   help="dB sobre el suelo de ruido para considerar ocupacion")
    p.add_argument("--intervalo", type=float, default=0.0,
                   help="pausa entre barridos en modo continuo (def. 0: "
                        "ventana deslizante sin pausas)")
    p.add_argument("--mapa", action="store_true",
                   help="muestra la persistencia bin a bin")
    p.add_argument("--json", action="store_true", help="salida estructurada")
    p.add_argument("--guardar", type=Path, default=None,
                   help="escribe el analisis completo a un fichero JSON")
    p.add_argument("--config", type=Path, default=CONFIG_DEF)
    p.add_argument("--clasificar-cada", type=float, default=CLASIF_CADA_S,
                   help="segundos entre capturas para el clasificador de UAV "
                        f"(def. {CLASIF_CADA_S:g}; 0 lo desactiva)")
    p.add_argument("--clasif-lna", type=int, default=32,
                   help="ganancia LNA de esas capturas; debe ser la del "
                        "entrenamiento del modelo")
    p.add_argument("--clasif-vga", type=int, default=32)
    args = p.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # El receptor se cierra pase lo que pase: un hackrf_sweep huerfano
    # dejaria el HackRF ocupado y el siguiente arranque del servicio no
    # podria abrirlo.
    try:
        if args.calibrar:
            return modo_calibrar(args)
        if args.continuo:
            return modo_continuo(args)
        return modo_informe(args)
    finally:
        cerrar_receptor()


if __name__ == "__main__":
    sys.exit(main())
