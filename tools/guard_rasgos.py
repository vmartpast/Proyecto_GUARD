#!/usr/bin/env python3
"""guard_rasgos: calcula los rasgos de cada ventana de 50 ms de las
capturas IQ (.cs8) de un dataset y los reune en rasgos.csv.
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np

NFFT = 256                 # 78 kHz por bin y 12,8 us por trama a 20 Msps
PROMEDIO = 4               # tramas promediadas: 51 us por fila. Sin esto,
                           # una celda suelta de ruido supera el umbral y
                           # cuenta como actividad (~20 % de las tramas)
UMBRAL_DB = 10.0           # actividad = mas de 10 dB sobre el suelo
BORDE = 0.08               # fraccion de banda descartada en cada borde
CENTRO_BINS = 2            # bins descartados a cada lado del centro
FIJA = 0.8                 # un bin activo en mas del 80 % de las filas es
                           # una portadora fija (p. ej. espuria del propio
                           # receptor), no una rafaga: se descarta
VERSION_RASGOS = "2"       # cambia la clave de cache al cambiar el calculo

NOMBRES = [
    "ocupacion",            # fraccion de celdas tiempo-frecuencia activas
    "tramas_activas",       # fraccion de tramas con alguna actividad
    "ancho_medio_mhz",      # ancho ocupado medio en tramas activas
    "ancho_p90_mhz",
    "ancho_max_mhz",
    "rafagas_por_s",
    "rafaga_media_ms",
    "rafaga_std_ms",
    "rafaga_max_ms",
    "intervalo_medio_ms",   # entre inicios de rafagas consecutivas
    "intervalo_cv",         # regularidad: desviacion / media
    "periodicidad",         # pico de autocorrelacion de la actividad
    "periodo_ms",           # retardo de ese pico
    "salto_medio_mhz",      # |diferencia| de centro entre rafagas
    "salto_std_mhz",
    "centros_distintos",    # canales distintos usados (rejilla de 1 MHz)
    "pico_sobre_suelo_db",
    "media_activa_db",      # potencia media de lo activo sobre el suelo
    "curtosis_envolvente",
    "planitud_espectral",
    "frac_rafagas_estrechas",   # rafagas de menos de 2 MHz
    "frac_rafagas_anchas",      # rafagas de mas de 10 MHz
]


def leer_cs8(ruta: Path, inicio: int, n: int) -> np.ndarray:
    """Lee un tramo de una captura .cs8 y lo devuelve como numeros complejos.

    @param  ruta    fichero .cs8
    @param  inicio  primera muestra a leer (en muestras, no en bytes)
    @param  n       numero de muestras complejas
    @return array complex64 con I + jQ
    """
    crudo = np.fromfile(ruta, dtype=np.int8, count=2 * n, offset=2 * inicio)
    x = crudo.astype(np.float32)
    return x[0::2] + 1j * x[1::2]


def espectrograma(x: np.ndarray) -> np.ndarray:
    """Calcula el espectrograma (potencia por tiempo y frecuencia) de un
    tramo.

    @param  x  tramo de senal compleja
    @return matriz float32 (filas = tiempo, columnas = frecuencia) con la potencia lineal de cada celda
    """
    x = x - x.mean()
    n = x.size // NFFT
    tramas = x[: n * NFFT].reshape(n, NFFT) * np.hanning(NFFT)
    p = np.abs(np.fft.fftshift(np.fft.fft(tramas, axis=1), axes=1)) ** 2
    n = (p.shape[0] // PROMEDIO) * PROMEDIO
    p = p[:n].reshape(-1, PROMEDIO, NFFT).mean(axis=1)
    return p.astype(np.float32)


def mascara_bins() -> np.ndarray:
    """Indica que columnas de frecuencia del espectrograma se analizan.

    @return array booleano de longitud NFFT (True = se usa)
    """
    m = np.ones(NFFT, dtype=bool)
    b = int(BORDE * NFFT)
    m[:b] = False
    m[-b:] = False
    c = NFFT // 2
    m[c - CENTRO_BINS: c + CENTRO_BINS + 1] = False
    return m


def rachas(v: np.ndarray):
    """Encuentra las rachas de valores verdaderos consecutivos.

    @param  v  array booleano
    @return tupla (inicios, longitudes), ambos arrays de enteros
    """
    d = np.diff(np.concatenate([[0], v.astype(np.int8), [0]]))
    ini = np.flatnonzero(d == 1)
    fin = np.flatnonzero(d == -1)
    return ini, fin - ini


def rasgos_ventana(x: np.ndarray, fs: float) -> np.ndarray:
    """Resume una ventana de senal en 22 numeros (los rasgos del
    clasificador).

    @param  x   ventana de senal compleja (p. ej. 50 ms = 1 M muestras)
    @param  fs  frecuencia de muestreo en muestras/s
    @return array float64 de 22 rasgos, en el orden de NOMBRES
    """
    p = espectrograma(x)[:, mascara_bins()]
    df_mhz = fs / NFFT / 1e6
    dt_ms = NFFT * PROMEDIO / fs * 1e3
    n_tramas, n_bins = p.shape

    # Suelo: mediana por bin en el tiempo y luego mediana entre bins.
    # La primera ignora rafagas cortas; la segunda, canales ocupados.
    suelo = float(np.median(np.median(p, axis=0))) + 1e-12
    db = 10 * np.log10(p / suelo + 1e-12)
    act = db > UMBRAL_DB
    # Portadoras fijas: el HackRF muestra una raya permanente en 2440 MHz
    # (armonico de su reloj de 40 MHz) que el USRP no tiene. Una raya que
    # no se apaga no es una emision a rafagas; si se dejara, el modelo
    # aprenderia una huella del receptor.
    act[:, act.mean(axis=0) > FIJA] = False

    ocupacion = act.mean()
    activa = act.any(axis=1)
    anchos = act.sum(axis=1)[activa] * df_mhz
    if anchos.size == 0:
        anchos = np.zeros(1)

    ini, lon = rachas(activa)
    dur_s = n_tramas * dt_ms / 1e3
    lon_ms = lon * dt_ms if lon.size else np.zeros(1)

    if ini.size > 1:
        inter = np.diff(ini) * dt_ms
        inter_medio = inter.mean()
        inter_cv = inter.std() / (inter.mean() + 1e-9)
    else:
        inter_medio, inter_cv = dur_s * 1e3, 0.0

    # Periodicidad de la actividad: autocorrelacion normalizada.
    a = activa.astype(np.float32) - activa.mean()
    if a.any():
        ac = np.fft.irfft(np.abs(np.fft.rfft(a, 2 * a.size)) ** 2)[: a.size]
        ac /= ac[0] + 1e-12
        # Por debajo de unos ms la autocorrelacion es alta solo porque una
        # rafaga se parece a si misma; lo que interesa es la repeticion.
        min_lag = max(1, int(3.0 / dt_ms))
        k = min_lag + int(np.argmax(ac[min_lag: a.size // 2]))
        periodicidad, periodo = float(ac[k]), k * dt_ms
    else:
        periodicidad, periodo = 0.0, 0.0

    # Centro de cada rafaga (relativo, solo se usan sus diferencias) y
    # ancho de cada rafaga.
    centros, anchos_r = [], []
    eje = np.arange(n_bins)
    for i0, l in zip(ini, lon):
        bloque = act[i0: i0 + l].any(axis=0)
        if bloque.any():
            w = (db[i0: i0 + l].clip(0) * act[i0: i0 + l]).sum(axis=0)
            centros.append((w * eje).sum() / (w.sum() + 1e-9) * df_mhz)
            anchos_r.append(bloque.sum() * df_mhz)
    centros = np.array(centros)
    anchos_r = np.array(anchos_r) if anchos_r else np.zeros(1)
    if centros.size > 1:
        saltos = np.abs(np.diff(centros))
        salto_medio, salto_std = saltos.mean(), saltos.std()
    else:
        salto_medio = salto_std = 0.0
    distintos = np.unique(np.round(centros)).size if centros.size else 0

    env = p.sum(axis=1)
    env_db = 10 * np.log10(env / (env.mean() + 1e-12) + 1e-12)
    curt = float(((env - env.mean()) ** 4).mean()
                 / ((env.var() + 1e-12) ** 2))

    espectro = p.mean(axis=0) + 1e-12
    planitud = float(np.exp(np.log(espectro).mean()) / espectro.mean())

    activos_db = db[act]
    return np.array([
        ocupacion,
        activa.mean(),
        anchos.mean(), np.percentile(anchos, 90), anchos.max(),
        ini.size / dur_s,
        lon_ms.mean(), lon_ms.std(), lon_ms.max(),
        inter_medio, inter_cv,
        periodicidad, periodo,
        salto_medio, salto_std, distintos,
        float(db.max()),
        float(activos_db.mean()) if activos_db.size else 0.0,
        np.log10(curt + 1e-9),
        planitud,
        float((anchos_r < 2).mean()),
        float((anchos_r > 10).mean()),
    ], dtype=np.float64)


def rasgos_captura(ruta: Path, r: dict, ventana_ms: float,
                   max_ventanas: int) -> list:
    """Trocea una captura en ventanas y calcula los rasgos de cada una.

    @param  ruta          fichero .cs8
    @param  r             registro de la captura en el manifiesto (etiqueta, muestreo, origen, receptor)
    @param  ventana_ms    duracion de cada ventana en milisegundos
    @param  max_ventanas  limite de ventanas por captura (0 = todas)
    @return lista de filas: etiqueta, captura, origen, receptor, indice de ventana y los 22 rasgos
    """
    fs = r["muestreo_sps"]
    n_v = int(fs * ventana_ms / 1000)
    n_ventanas = (ruta.stat().st_size // 2) // n_v
    if max_ventanas:
        n_ventanas = min(n_ventanas, max_ventanas)
    captura = ruta.stem
    filas = []
    for k in range(n_ventanas):
        v = rasgos_ventana(leer_cs8(ruta, k * n_v, n_v), fs)
        filas.append([r["etiqueta"], captura, r.get("origen", "propio"),
                      r.get("receptor", ""), k] + [f"{c:.6g}" for c in v])
    return filas


def procesar(raiz: Path, ventana_ms: float, salida: Path,
             max_ventanas: int, limite_s: float) -> int:
    """Calcula los rasgos de todas las capturas del manifiesto.

    @param  raiz          carpeta del dataset (con manifest.jsonl)
    @param  ventana_ms    duracion de cada ventana en milisegundos
    @param  salida        CSV final con todas las ventanas
    @param  max_ventanas  limite de ventanas por captura (0 = todas)
    @param  limite_s      segundos maximos de trabajo (0 = sin limite)
    @return 0 si termino, 2 si quedan capturas pendientes
    """
    registros = [json.loads(l) for l in open(raiz / "manifest.jsonl")
                 if l.strip()]
    # Una captura convertida dos veces aparece dos veces: vale la ultima.
    por_fichero = {r["fichero"]: r for r in registros}
    cache = raiz / "rasgos"
    cache.mkdir(exist_ok=True)
    cabecera = ["etiqueta", "captura", "origen", "receptor", "ventana"] \
        + NOMBRES
    clave = f"v{ventana_ms:g}ms_n{NFFT}x{PROMEDIO}_r{VERSION_RASGOS}"

    t0 = time.monotonic()
    pendientes = 0
    for rel, r in sorted(por_fichero.items()):
        ruta = raiz / rel
        if not ruta.is_file():
            print(f"  falta {rel}, se omite")
            continue
        dest = cache / f"{ruta.stem}.{clave}.csv"
        if dest.is_file() and dest.stat().st_mtime >= ruta.stat().st_mtime:
            continue
        if limite_s and time.monotonic() - t0 > limite_s:
            pendientes += 1
            continue
        filas = rasgos_captura(ruta, r, ventana_ms, max_ventanas)
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cabecera)
            w.writerows(filas)
        tmp.replace(dest)
        print(f"  {ruta.stem}: {len(filas)} ventanas", flush=True)

    if pendientes:
        print(f"quedan {pendientes} capturas: vuelve a lanzarlo")
        return 2

    total = 0
    with open(salida, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cabecera)
        for rel in sorted(por_fichero):
            dest = cache / f"{Path(rel).stem}.{clave}.csv"
            if dest.is_file():
                filas = list(csv.reader(open(dest)))[1:]
                w.writerows(filas)
                total += len(filas)
    print(f"{total} ventanas -> {salida}")
    return 0


def main() -> int:
    """Punto de entrada: interpreta la linea de ordenes y lanza procesar().

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Rasgos por ventana del dataset")
    p.add_argument("dataset", type=Path)
    p.add_argument("--ventana-ms", type=float, default=50.0)
    p.add_argument("--max-ventanas", type=int, default=0,
                   help="limite por captura (0 = todas)")
    p.add_argument("--salida", type=Path, default=None)
    p.add_argument("--limite-s", type=float, default=0,
                   help="parar tras N segundos (se reanuda al relanzar)")
    a = p.parse_args()
    salida = a.salida or a.dataset / "rasgos.csv"
    return procesar(a.dataset, a.ventana_ms, salida, a.max_ventanas,
                    a.limite_s)


if __name__ == "__main__":
    sys.exit(main())
