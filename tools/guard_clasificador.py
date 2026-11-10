#!/usr/bin/env python3
"""guard_clasificador: clasificador de UAV en tiempo real. Clasifica las
capturas IQ que pide el detector, confirma las dudosas y publica las alarmas.
"""

import argparse
import json
import os
import signal
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import guard_rasgos as gr

PETICION_FILE = Path("/run/guard/clasif_peticion.json")
CONFIRMAR_FILE = Path("/run/guard/clasif_confirmar.json")
RESULTADO_FILE = Path("/run/guard/clasificacion.json")
HEALTH_FILE = Path("/run/guard/clasificador.health")
MODELO_DEF = Path("/opt/guard_device/modelo/modelo_guard.npz")
NEGATIVA = "sin_uav"
VENTANA_MS = 50.0
HISTORIAL = 10              # resultados recientes que se publican
# Niveles de una captura segun la fraccion de ventanas de la clase
# ganadora (si es un UAV):
#   >= UMBRAL_FUERTE              alarma directa
#   >= UMBRAL_POSIBLE y < FUERTE  "posible": hace falta confirmarla
# Medido con guard_reglas.py sobre la evaluacion (capturas de 0,5 s): el
# 74 % de las capturas de UAV salen fuertes y el 12 % posibles; de las
# capturas sin UAV externas, el 7 % y el 7 %. La confirmacion recupera
# casi todos los UAV posibles y frena la mayoria de las dudas aisladas.
UMBRAL_POSIBLE = 0.5
UMBRAL_FUERTE = 0.7
# Una captura posible se confirma con otra posible o fuerte de la misma
# zona dentro de este plazo. El detector repite la captura en el barrido
# siguiente, asi que en la practica llega en unos 4-5 s.
CONFIRMA_S = 30.0
CONFIRMA_MHZ = 10.0

_running = True


def _stop(signum, frame):
    """Detiene el bucle principal al recibir SIGTERM o SIGINT.

    @param  signum  senal recibida
    @param  frame   marco de pila actual (no se usa)
    """
    global _running
    _running = False


def now_iso() -> str:
    """Devuelve la fecha y hora actuales en UTC.

    @return fecha y hora UTC en ISO 8601 con milisegundos
    """
    a = datetime.now(timezone.utc)
    return a.strftime("%Y-%m-%dT%H:%M:%S.") + f"{a.microsecond // 1000:03d}Z"


def emit(obj: dict) -> None:
    """Escribe un evento JSON por la salida estandar (va al diario).

    @param  obj  evento; el puente reenvia al ESP32 los de tipo "detection"
    """
    sys.stdout.write(json.dumps(obj, separators=(",", ":"),
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def escribir_atomico(ruta: Path, datos: dict) -> None:
    """Escribe un JSON de forma atomica (temporal y renombrado) para que
    el panel nunca lea un fichero a medias.

    @param  ruta   fichero de destino
    @param  datos  contenido a escribir
    """
    tmp = ruta.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(datos, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, ruta)


class Modelo:
    """Bosque aleatorio exportado por guard_modelo.py, evaluado con numpy."""

    def __init__(self, ruta: Path):
        """Carga los arboles del modelo desde el fichero .npz.

        @param  ruta  fichero .npz del modelo
        """
        d = np.load(ruta, allow_pickle=False)
        self.raiz = d["raiz"]
        self.rasgo = d["rasgo"].astype(np.int64)
        self.umbral = d["umbral"]
        self.izq = d["izq"].astype(np.int64)
        self.der = d["der"].astype(np.int64)
        self.votos = d["votos"]
        self.clases = [str(c) for c in d["clases"]]
        self.meta = json.loads(str(d["meta"]))
        self.rasgos = self.meta["rasgos"]
        idx = {n: i for i, n in enumerate(gr.NOMBRES)}
        faltan = [n for n in self.rasgos if n not in idx]
        if faltan:
            raise KeyError(f"rasgos desconocidos para guard_rasgos: {faltan}")
        # Columnas de guard_rasgos que usa el modelo, en su orden
        self.columnas = np.array([idx[n] for n in self.rasgos])

    def probabilidades(self, X: np.ndarray) -> np.ndarray:
        """Probabilidad media de cada clase segun los arboles.

        @param  X  matriz ventanas x 22 rasgos (orden de guard_rasgos)
        @return matriz ventanas x clases
        """
        X = np.asarray(X[:, self.columnas], dtype=np.float32).astype(np.float64)
        prob = np.zeros((len(X), len(self.clases)))
        filas = np.arange(len(X))
        for r0 in self.raiz:
            nodo = np.full(len(X), r0, dtype=np.int64)
            activo = self.rasgo[nodo] >= 0
            while activo.any():
                n = nodo[activo]
                ir_izq = X[filas[activo], self.rasgo[n]] <= self.umbral[n]
                nodo[activo] = np.where(ir_izq, self.izq[n], self.der[n])
                activo = self.rasgo[nodo] >= 0
            prob += self.votos[nodo]
        return prob / len(self.raiz)


def clasificar_fichero(modelo: Modelo, ruta: Path, fs: float) -> dict:
    """Clasifica una captura IQ completa.

    @param  modelo  Modelo cargado
    @param  ruta    fichero .cs8
    @param  fs      frecuencia de muestreo en muestras/s
    @return diccionario con la clase decidida, la confianza, el nivel, los votos por clase, la probabilidad media y la latencia de cada etapa
    """
    t0 = time.perf_counter()
    n_v = int(fs * VENTANA_MS / 1000)
    n_ventanas = (ruta.stat().st_size // 2) // n_v
    if n_ventanas == 0:
        raise ValueError("captura demasiado corta")
    filas = [gr.rasgos_ventana(gr.leer_cs8(ruta, k * n_v, n_v), fs)
             for k in range(n_ventanas)]
    X = np.vstack(filas)
    t1 = time.perf_counter()
    prob = modelo.probabilidades(X)
    t2 = time.perf_counter()

    pred = [modelo.clases[i] for i in prob.argmax(axis=1)]
    votos = Counter(pred)
    clase, n = votos.most_common(1)[0]
    confianza = n / len(pred)
    nivel = nivel_captura(clase, confianza)
    return {
        "clase": clase,
        "confianza": round(confianza, 3),
        "nivel": nivel,
        "alarma": nivel == "fuerte",
        "votos": dict(votos),
        "prob_media": {c: round(float(p), 3)
                       for c, p in zip(modelo.clases, prob.mean(axis=0))},
        "ventanas": len(pred),
        "latencia_ms": {"rasgos": round(1000 * (t1 - t0), 1),
                        "modelo": round(1000 * (t2 - t1), 1),
                        "total": round(1000 * (t2 - t0), 1)},
    }


def nivel_captura(clase: str, confianza: float) -> str:
    """Asigna el nivel de confianza de una captura segun su clase y la
    fraccion de ventanas que la votaron.

    @param  clase      clase mas votada de la captura
    @param  confianza  fraccion de ventanas que la votaron
    @return "negativo", "posible" o "fuerte"
    """
    if clase == NEGATIVA or confianza < UMBRAL_POSIBLE:
        return "negativo"
    return "fuerte" if confianza >= UMBRAL_FUERTE else "posible"


class Confirmador:
    """Recuerda la ultima captura "posible" y decide si la siguiente de la
    misma zona la confirma o la descarta.
    """

    def __init__(self):
        """Crea el confirmador sin ninguna captura pendiente."""
        self.pendiente = None

    def misma_zona(self, f) -> bool:
        """Comprueba si una frecuencia esta cerca de la captura pendiente y
        dentro del plazo de confirmacion.

        @param  f  frecuencia central de la captura nueva en MHz
        @return True si esta en la misma zona y a tiempo
        """
        p = self.pendiente
        return (p is not None and f is not None and p["f"] is not None
                and time.monotonic() - p["t"] <= CONFIRMA_S
                and abs(float(f) - float(p["f"])) <= CONFIRMA_MHZ)

    def decidir(self, res: dict) -> None:
        """Completa res con la decision final: "alarma" y "confirmacion" (None,
        "pendiente", "confirmada" o "descartada").

        @param  res  resultado de clasificar_fichero con f_centro_mhz e id
        """
        f = res.get("f_centro_mhz")
        nv = res["nivel"]
        res["confirmacion"] = None
        if nv == "fuerte":
            if self.misma_zona(f):
                res["confirmacion"] = "confirmada"
            self.pendiente = None
            res["alarma"] = True
        elif nv == "posible":
            if self.misma_zona(f):
                res["confirmacion"] = "confirmada"
                res["confirma_a"] = {k: self.pendiente[k] for k in
                                     ("clase", "confianza", "id")}
                self.pendiente = None
                res["alarma"] = True
            else:
                self.pendiente = {"t": time.monotonic(), "f": f,
                                  "clase": res["clase"],
                                  "confianza": res["confianza"],
                                  "id": res.get("id")}
                res["confirmacion"] = "pendiente"
                res["alarma"] = False
                pedir_confirmacion(f, res.get("id"))
        else:
            if self.misma_zona(f):
                res["confirmacion"] = "descartada"
                res["descarta_a"] = {k: self.pendiente[k] for k in
                                     ("clase", "confianza", "id")}
                self.pendiente = None
            res["alarma"] = False


def pedir_confirmacion(f_mhz, id_captura) -> None:
    """Pide al detector una captura inmediata en la misma frecuencia.

    @param  f_mhz       centro de la captura dudosa
    @param  id_captura  id de esa captura, para el diario
    """
    try:
        escribir_atomico(CONFIRMAR_FILE, {"f_centro_mhz": f_mhz,
                                          "id": id_captura, "ts": now_iso()})
    except OSError:
        pass


def borrar_captura(ruta: Path) -> None:
    """Borra la captura .cs8 y su .json, que ocupan memoria en tmpfs.

    @param  ruta  fichero .cs8 de la captura
    """
    for f in (ruta, ruta.with_suffix(".json")):
        try:
            f.unlink()
        except OSError:
            pass


def bucle(modelo: Modelo) -> None:
    """Espera peticiones del detector, clasifica y publica.

    @param  modelo  Modelo cargado
    """
    historial = deque(maxlen=HISTORIAL)
    conf = Confirmador()
    emit({"ts": now_iso(), "type": "status", "mode": "clasificador",
          "modelo": modelo.meta.get("fecha"), "clases": modelo.clases,
          "rasgos": len(modelo.rasgos), "arboles": len(modelo.raiz)})
    while _running:
        try:
            HEALTH_FILE.touch()
        except OSError:
            pass
        if not PETICION_FILE.is_file():
            time.sleep(0.2)
            continue
        try:
            pet = json.loads(PETICION_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pet = None
        try:
            PETICION_FILE.unlink()
        except OSError:
            pass
        if not pet or "fichero" not in pet:
            continue

        ruta = Path(pet["fichero"])
        try:
            meta_iq = json.loads(ruta.with_suffix(".json").read_text())
            fs = float(meta_iq.get("muestreo_sps", 20e6))
            res = clasificar_fichero(modelo, ruta, fs)
        except Exception as exc:      # noqa: BLE001 — se informa y se sigue
            emit({"ts": now_iso(), "type": "error", "mode": "clasificador",
                  "msg": f"no se pudo clasificar {ruta.name}: "
                         f"{str(exc)[:200]}"})
            borrar_captura(ruta)
            continue
        borrar_captura(ruta)

        res.update({"ts": now_iso(), "id": pet.get("id"),
                    "f_centro_mhz": pet.get("f_centro_mhz"),
                    "eleccion": pet.get("motivo"),
                    "ganancia": [pet.get("ganancia_lna_db"),
                                 pet.get("ganancia_vga_db")]})
        conf.decidir(res)
        historial.appendleft({k: res[k] for k in
                              ("ts", "f_centro_mhz", "clase", "confianza",
                               "nivel", "confirmacion", "alarma")})
        escribir_atomico(RESULTADO_FILE, {
            **res, "historial": list(historial),
            "modelo": {"fecha": modelo.meta.get("fecha"),
                       "arboles": len(modelo.raiz),
                       "rasgos": len(modelo.rasgos)}})
        emit({"ts": res["ts"], "type": "clasificacion",
              "clase": res["clase"], "confianza": res["confianza"],
              "nivel": res["nivel"], "confirmacion": res["confirmacion"],
              "eleccion": res["eleccion"],
              "f_centro_mhz": res["f_centro_mhz"],
              "latencia_ms": res["latencia_ms"]["total"]})
        if res["alarma"]:
            # Mismo formato de evento que el detector: el puente lo
            # reenvia al ESP32 como alerta sin necesitar cambios de fondo.
            emit({"ts": res["ts"], "type": "detection", "confirmed": True,
                  "source": "clasificador", "model": res["clase"],
                  "confidence": res["confianza"], "rssi_dbfs": None,
                  "f_centro_mhz": res["f_centro_mhz"]})


def main() -> int:
    """Punto de entrada: modo servicio o clasificacion de un fichero suelto.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Clasificador de UAV")
    p.add_argument("--modelo", type=Path, default=MODELO_DEF)
    p.add_argument("--fichero", type=Path, default=None,
                   help="clasifica este .cs8 y termina (prueba)")
    p.add_argument("--muestreo", type=float, default=20e6)
    a = p.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Sin modelo no se puede clasificar, pero el servicio no debe entrar en
    # un bucle de reinicios: se publica el estado y se reintenta despacio.
    while True:
        try:
            modelo = Modelo(a.modelo)
            break
        except (OSError, KeyError, ValueError) as exc:
            if a.fichero:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            emit({"ts": now_iso(), "type": "error", "mode": "clasificador",
                  "msg": f"modelo no disponible ({a.modelo}): {exc}"})
            try:
                escribir_atomico(RESULTADO_FILE, {"ts": now_iso(),
                                                  "estado": "sin_modelo"})
            except OSError:
                pass
            for _ in range(300):
                if not _running:
                    return 0
                time.sleep(0.1)

    if a.fichero:
        res = clasificar_fichero(modelo, a.fichero, a.muestreo)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    bucle(modelo)
    emit({"ts": now_iso(), "type": "status", "mode": "clasificador",
          "stopping": True})
    return 0


if __name__ == "__main__":
    sys.exit(main())
