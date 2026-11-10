#!/usr/bin/env python3
"""guard_spectrum: version previa del detector basada en la energia,
conservada como herramienta de diagnostico. Detecta y clasifica las
emisiones de un barrido acumulado y aplica la lista blanca.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BANDA_MIN_MHZ = 2400
BANDA_MAX_MHZ = 2500
ANCHO_BIN_HZ = 1_000_000

# Margen sobre el suelo de ruido a partir del cual se considera emision.
# 10 dB deja holgura frente a la variabilidad del propio suelo (±3 dB
# observados) sin perder emisiones debiles.
UMBRAL_SOBRE_RUIDO_DB = 10.0

# Una emision aislada de 1 MHz suele ser ruido impulsivo. Se exige
# continuidad para considerarla real.
MIN_BINS_CONTIGUOS = 2

DURACION_BARRIDO_S = 3.0
HEALTH_FILE = Path("/run/guard/detector.health")
CONFIG_DEF = Path("/opt/guard_device/config/lista_blanca.json")

# Canales WiFi de 2,4 GHz: centro en MHz. Ocupan ~20 MHz.
CANALES_WIFI = {
    1: 2412, 2: 2417, 3: 2422, 4: 2427, 5: 2432, 6: 2437,
    7: 2442, 8: 2447, 9: 2452, 10: 2457, 11: 2462, 12: 2467,
    13: 2472, 14: 2484,
}
ANCHO_WIFI_MHZ = 20
TOLERANCIA_WIFI_MHZ = 4

_running = True


def _stop(signum, frame):
    """Detiene el bucle principal al recibir SIGTERM o SIGINT.

    @param  signum  senal recibida
    @param  frame   marco de pila actual (no se usa)
    """
    global _running
    _running = False


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


def touch_health() -> None:
    """Actualiza el fichero de latido que vigila el puente."""
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.touch()
    except OSError:
        pass


# Barrido

def barrer(f_min: int, f_max: int, ancho_bin: int,
           duracion: float) -> dict[int, float] | None:
    """Ejecuta hackrf_sweep y conserva el maximo de cada bin entre las
    pasadas del barrido.

    @param  f_min      frecuencia inicial en MHz
    @param  f_max      frecuencia final en MHz
    @param  ancho_bin  ancho de bin en Hz
    @param  duracion   duracion del barrido en segundos
    @return diccionario frecuencia en MHz -> potencia maxima en dB
    """
    cmd = [
        "hackrf_sweep",
        "-f", f"{f_min}:{f_max}",
        "-w", str(ancho_bin),
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"no se pudo ejecutar hackrf_sweep: {exc}"})
        return None

    potencias: dict[int, float] = {}
    t0 = time.monotonic()

    try:
        for linea in proc.stdout:
            if time.monotonic() - t0 > duracion:
                break
            campos = linea.strip().split(", ")
            if len(campos) < 7:
                continue
            try:
                f_ini = int(campos[2])
                paso = float(campos[4])
                for k, valor in enumerate(campos[6:]):
                    f_mhz = int((f_ini + k * paso) / 1e6)
                    db = float(valor)
                    if f_mhz not in potencias or db > potencias[f_mhz]:
                        potencias[f_mhz] = db
            except (ValueError, IndexError):
                continue
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    return potencias if potencias else None


# Analisis

def mediana(valores: list[float]) -> float:
    """Calcula la mediana de una lista de valores.

    @param  valores  lista de valores
    @return mediana
    """
    v = sorted(valores)
    n = len(v)
    if n == 0:
        return 0.0
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def suelo_de_ruido(potencias: dict[int, float]) -> float:
    """Estima el suelo de ruido como la mediana de la potencia de la banda.

    @param  potencias  diccionario frecuencia -> potencia en dB
    @return suelo de ruido en dB
    """
    return mediana(list(potencias.values()))


def agrupar_emisiones(potencias: dict[int, float], umbral: float,
                      min_bins: int) -> list[dict]:
    """Agrupa los bins contiguos que superan el umbral en emisiones.

    @param  potencias  diccionario frecuencia -> potencia en dB
    @param  umbral     umbral de deteccion en dB
    @param  min_bins   numero minimo de bins de una emision
    @return lista de emisiones
    """
    frecuencias = sorted(potencias)
    emisiones = []
    actual: list[int] = []

    for f in frecuencias:
        if potencias[f] >= umbral:
            if actual and f != actual[-1] + 1:
                emisiones.append(actual)
                actual = []
            actual.append(f)
        elif actual:
            emisiones.append(actual)
            actual = []
    if actual:
        emisiones.append(actual)

    resultado = []
    for grupo in emisiones:
        if len(grupo) < min_bins:
            continue
        picos = [potencias[f] for f in grupo]
        pico_max = max(picos)
        f_pico = grupo[picos.index(pico_max)]
        # Ancho a -3 dB del pico: criterio estandar en RF. El ancho a
        # umbral fijo depende del nivel de senal y subestima las emisiones
        # debiles, cuyas faldas quedan bajo el umbral.
        limite_3db = pico_max - 3.0
        bins_3db = [f for f in grupo if potencias[f] >= limite_3db]
        ancho_3db = len(bins_3db) if bins_3db else 1

        resultado.append({
            "f_inicio_mhz": grupo[0],
            "f_fin_mhz": grupo[-1],
            "ancho_mhz": len(grupo),
            "ancho_3db_mhz": ancho_3db,
            "f_centro_3db_mhz": ((bins_3db[0] + bins_3db[-1]) / 2
                                 if bins_3db else f_pico),
            "f_centro_mhz": (grupo[0] + grupo[-1]) / 2,
            "f_pico_mhz": f_pico,
            "pico_db": round(pico_max, 1),
        })
    return resultado


def clasificar(emision: dict, suelo: float) -> tuple[str, str]:
    """Clasifica una emision por su ancho y su posicion en la banda.

    @param  emision  emision a clasificar
    @param  suelo    suelo de ruido en dB
    @return tupla (tipo, detalle)
    """
    # Se clasifica por el ancho total sobre umbral, no por el ancho a
    # -3 dB. Medido experimentalmente: el ancho a -3 dB es relativo al
    # pico y se estrecha cuando la senal se refuerza (3-6 MHz observados
    # para la misma emision segun su carga), mientras que el ancho sobre
    # umbral se mantuvo constante en 11 MHz en todas las medidas.
    ancho = emision["ancho_mhz"]
    centro = emision["f_centro_mhz"]

    # Un canal WiFi de 20 MHz nominales ocupa unos 10-14 MHz por encima
    # de un umbral situado 10 dB sobre el suelo de ruido: las faldas del
    # canal quedan por debajo.
    if 8 <= ancho <= 18:
        # Canal mas proximo, no el primero que entra en tolerancia.
        candidatos = [(abs(centro - f), c) for c, f in CANALES_WIFI.items()
                      if abs(centro - f) <= TOLERANCIA_WIFI_MHZ]
        if candidatos:
            _, canal = min(candidatos)
            return "wifi", f"canal {canal}"
        return "banda_media", "ancho compatible con WiFi, centro no normalizado"

    if ancho <= 2:
        return "banda_estrecha", "compatible con salto de frecuencia"

    if ancho >= 25:
        return "banda_ancha", "ocupacion amplia"

    return "desconocido", ""


# Lista blanca

def cargar_lista_blanca(ruta: Path) -> list[dict]:
    """Lee la lista blanca de emisores conocidos.

    @param  ruta  fichero JSON de la lista blanca
    @return lista de emisores, o vacia si no existe o no es valido
    """
    if not ruta.is_file():
        return []
    try:
        with open(ruta) as f:
            datos = json.load(f)
        return datos.get("emisores", [])
    except (OSError, json.JSONDecodeError) as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"lista blanca ilegible: {exc}"})
        return []


def es_conocido(emision: dict, lista: list[dict]) -> str | None:
    """Busca una emision en la lista blanca.

    @param  emision  emision a comprobar
    @param  lista    lista de emisores conocidos
    @return etiqueta del emisor conocido, o None
    """
    for entrada in lista:
        f_ini = entrada.get("f_inicio_mhz", 0)
        f_fin = entrada.get("f_fin_mhz", 0)
        # Solapamiento con el rango declarado
        if emision["f_fin_mhz"] >= f_ini and emision["f_inicio_mhz"] <= f_fin:
            return entrada.get("etiqueta", "sin etiqueta")
    return None


# Funciones

def analizar(potencias: dict[int, float], lista: list[dict],
             margen: float, min_bins: int) -> dict:
    """Analiza un barrido: suelo de ruido, emisiones, clasificacion y
    lista blanca.

    @param  potencias  diccionario frecuencia -> potencia en dB
    @param  lista      lista de emisores conocidos
    @param  margen     margen sobre el suelo de ruido en dB
    @param  min_bins   numero minimo de bins de una emision
    @return diccionario con el analisis
    """
    suelo = suelo_de_ruido(potencias)
    umbral = suelo + margen
    emisiones = agrupar_emisiones(potencias, umbral, min_bins)

    for e in emisiones:
        tipo, detalle = clasificar(e, suelo)
        e["tipo"] = tipo
        e["detalle"] = detalle
        e["snr_db"] = round(e["pico_db"] - suelo, 1)
        etiqueta = es_conocido(e, lista)
        e["conocido"] = etiqueta is not None
        e["etiqueta"] = etiqueta

    return {
        "suelo_ruido_db": round(suelo, 1),
        "umbral_db": round(umbral, 1),
        "bins": len(potencias),
        "emisiones": emisiones,
    }


def imprimir_informe(analisis: dict) -> None:
    """Muestra en texto el resultado del analisis.

    @param  analisis  resultado de analizar
    """
    print(f"\n  suelo de ruido : {analisis['suelo_ruido_db']:.1f} dB")
    print(f"  umbral         : {analisis['umbral_db']:.1f} dB")
    print(f"  bins analizados: {analisis['bins']}")

    emisiones = analisis["emisiones"]
    if not emisiones:
        print("\n  Sin emisiones por encima del umbral.")
        return

    print(f"\n  {len(emisiones)} emisión(es) detectada(s):\n")
    print(f"  {'banda (MHz)':<14} {'-3dB':>6} {'total':>6} {'pico':>8} "
          f"{'SNR':>6}  {'tipo':<38} {'estado'}")
    print(f"  {'-' * 14} {'-' * 6} {'-' * 6} {'-' * 8} {'-' * 6}  "
          f"{'-' * 38} {'-' * 16}")

    for e in emisiones:
        banda = f"{e['f_inicio_mhz']}-{e['f_fin_mhz']}"
        tipo = e["tipo"]
        if e["detalle"]:
            tipo = f"{tipo} ({e['detalle']})"
        estado = f"CONOCIDO: {e['etiqueta']}" if e["conocido"] else "NO IDENTIFICADO"
        print(f"  {banda:<14} {e['ancho_3db_mhz']:>4} MHz {e['ancho_mhz']:>4} MHz "
              f"{e['pico_db']:>7.1f} {e['snr_db']:>5.1f}  {tipo:<38} {estado}")


def emitir_eventos(analisis: dict) -> int:
    """Emite un evento por cada emision no identificada.

    @param  analisis  resultado de analizar
    @return numero de eventos emitidos
    """
    n = 0
    for e in analisis["emisiones"]:
        if e["conocido"]:
            continue
        emit({
            "ts": now_iso(),
            "type": "detection",
            "confirmed": True,
            "label": "rf_no_identificada",
            "model": None,
            "confidence": min(0.99, round(e["snr_db"] / 30.0, 2)),
            "window_s": DURACION_BARRIDO_S,
            "f_centro_mhz": e["f_centro_mhz"],
            "ancho_mhz": e["ancho_mhz"],
            "rssi_dbfs": e["pico_db"],
            "tipo": e["tipo"],
        })
        n += 1
    return n


def modo_calibrar(args) -> int:
    """Caracteriza el entorno y propone una lista blanca inicial.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    print("=" * 70)
    print("  GUARD — calibración del entorno radioeléctrico")
    print("=" * 70)
    print(f"\n  Barriendo {args.f_min}-{args.f_max} MHz durante "
          f"{args.duracion:.0f} s...")
    print("  (asegúrate de que NO hay emisores de interés activos)")

    potencias = barrer(args.f_min, args.f_max, args.ancho_bin, args.duracion)
    if not potencias:
        print("\n  ERROR: el barrido no devolvió datos.", file=sys.stderr)
        return 1

    analisis = analizar(potencias, [], args.margen, args.min_bins)
    imprimir_informe(analisis)

    propuesta = {
        "generada": now_iso(),
        "nota": "Revisar antes de usar. Cada entrada silencia esa banda.",
        "emisores": [
            {
                "etiqueta": (f"{e['tipo']} {e['detalle']}".strip()
                             or "emisor sin identificar"),
                "f_inicio_mhz": e["f_inicio_mhz"],
                "f_fin_mhz": e["f_fin_mhz"],
            }
            for e in analisis["emisiones"]
        ],
    }

    args.config.parent.mkdir(parents=True, exist_ok=True)
    destino = args.config.with_suffix(".propuesta.json")
    with open(destino, "w") as f:
        json.dump(propuesta, f, indent=2, ensure_ascii=False)

    print(f"\n  Propuesta de lista blanca en:\n    {destino}")
    print(f"\n  Revísala y, si procede:\n    mv {destino} {args.config}")
    return 0


def modo_una_vez(args) -> int:
    """Realiza un unico barrido y muestra el informe.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    lista = cargar_lista_blanca(args.config)
    print("=" * 70)
    print("  GUARD — barrido espectral")
    print("=" * 70)
    print(f"\n  banda          : {args.f_min}-{args.f_max} MHz")
    print(f"  resolución     : {args.ancho_bin / 1e6:.1f} MHz por bin")
    print(f"  lista blanca   : {len(lista)} emisor(es) declarado(s)")

    potencias = barrer(args.f_min, args.f_max, args.ancho_bin, args.duracion)
    if not potencias:
        print("\n  ERROR: el barrido no devolvió datos.", file=sys.stderr)
        return 1

    analisis = analizar(potencias, lista, args.margen, args.min_bins)
    imprimir_informe(analisis)
    return 0


def modo_continuo(args) -> int:
    """Modo servicio: barre la banda en bucle y emite los eventos.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    lista = cargar_lista_blanca(args.config)
    emit({"ts": now_iso(), "type": "status", "mode": "spectrum",
          "pid": os.getpid(), "banda_mhz": [args.f_min, args.f_max],
          "lista_blanca": len(lista)})

    ultimo_status = time.monotonic()

    while _running:
        touch_health()
        potencias = barrer(args.f_min, args.f_max, args.ancho_bin,
                           args.duracion)
        if potencias:
            analisis = analizar(potencias, lista, args.margen, args.min_bins)
            emitir_eventos(analisis)

            if time.monotonic() - ultimo_status >= 30:
                emit({"ts": now_iso(), "type": "status", "mode": "spectrum",
                      "suelo_ruido_db": analisis["suelo_ruido_db"],
                      "emisiones": len(analisis["emisiones"])})
                ultimo_status = time.monotonic()

        time.sleep(max(0.0, args.intervalo))

    emit({"ts": now_iso(), "type": "status", "mode": "spectrum",
          "stopping": True})
    return 0


def main() -> int:
    """Lee los argumentos y ejecuta el modo elegido.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Detector de actividad RF no identificada")
    modo = p.add_mutually_exclusive_group(required=True)
    modo.add_argument("--una-vez", action="store_true",
                      help="un barrido, informe por consola")
    modo.add_argument("--continuo", action="store_true",
                      help="barrido continuo, eventos JSON Lines")
    modo.add_argument("--calibrar", action="store_true",
                      help="caracteriza el entorno y propone lista blanca")

    p.add_argument("--f-min", type=int, default=BANDA_MIN_MHZ)
    p.add_argument("--f-max", type=int, default=BANDA_MAX_MHZ)
    p.add_argument("--ancho-bin", type=int, default=ANCHO_BIN_HZ)
    p.add_argument("--duracion", type=float, default=DURACION_BARRIDO_S,
                   help="segundos de acumulación por barrido")
    p.add_argument("--margen", type=float, default=UMBRAL_SOBRE_RUIDO_DB,
                   help="dB sobre el suelo de ruido para considerar emisión")
    p.add_argument("--min-bins", type=int, default=MIN_BINS_CONTIGUOS)
    p.add_argument("--intervalo", type=float, default=1.0,
                   help="pausa entre barridos en modo continuo")
    p.add_argument("--config", type=Path, default=CONFIG_DEF)
    args = p.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.calibrar:
        return modo_calibrar(args)
    if args.una_vez:
        return modo_una_vez(args)
    return modo_continuo(args)


if __name__ == "__main__":
    sys.exit(main())
