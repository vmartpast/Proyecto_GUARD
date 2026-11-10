#!/usr/bin/env python3
"""guard_espectrograma: genera espectrogramas de la banda completa (desde
barridos de hackrf_sweep) o de una captura IQ (.cs8), en .npy y .png.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Las dos dependencias se importan sin abortar. Este modulo lo importa
# tambien el panel para dibujar la captura fina, y un panel no puede
# morir porque falte una biblioteca: tiene que decir que falta y seguir.
try:
    import numpy as np
except ImportError:
    np = None

SALIDA_DEF = Path("/var/lib/guard/espectrogramas")

# Filas de la imagen final en modo iq. Un segundo a 20 Msps con FFT de
# 1024 son casi 20 000 tramas: una imagen de 20 000 filas no se puede
# mirar y no la acepta ninguna red. Se reduce agrupando tramas.
FILAS_IQ = 1024


# Comun

def guardar(matriz, nombre: Path, titulo: str, extent: tuple,
            eje_y: str) -> None:
    """Guarda la matriz del espectrograma en .npy y, si hay matplotlib,
    como imagen.

    @param  matriz  matriz tiempo x frecuencia en dB
    @param  nombre  ruta de salida sin extension
    @param  titulo  titulo de la imagen
    @param  extent  limites de los ejes para la imagen
    @param  eje_y   rotulo del eje vertical
    """
    nombre.parent.mkdir(parents=True, exist_ok=True)
    np.save(nombre.with_suffix(".npy"), matriz.astype(np.float32))
    print(f"  matriz   {nombre.with_suffix('.npy')}  {matriz.shape}")

    # matplotlib se importa aqui y no arriba: tarda uno o dos segundos en
    # cargar en la Pi, y el panel, que tambien importa este modulo, nunca
    # genera imagenes.
    try:
        import matplotlib
        matplotlib.use("Agg")      # sin pantalla: la Pi corre sin escritorio
        import matplotlib.pyplot as plt
    except ImportError:
        print("  (sin matplotlib: no se genera imagen)")
        return

    # Escala de color recortada a percentiles, no a minimo y maximo. Un
    # solo pico muy fuerte estiraria la escala y dejaria todo lo demas
    # del mismo color oscuro.
    vmin, vmax = np.percentile(matriz, [5, 99.7])

    fig, ax = plt.subplots(figsize=(12, 7), dpi=110)
    im = ax.imshow(matriz, aspect="auto", origin="upper", cmap="viridis",
                   vmin=vmin, vmax=vmax, extent=extent,
                   interpolation="nearest")
    ax.set_xlabel("frecuencia (MHz)")
    ax.set_ylabel(eje_y)
    ax.set_title(titulo)
    fig.colorbar(im, ax=ax, label="dBFS")
    fig.tight_layout()
    fig.savefig(nombre.with_suffix(".png"))
    plt.close(fig)
    print(f"  imagen   {nombre.with_suffix('.png')}")


def receptor_ocupado() -> bool:
    """Comprueba si el detector esta usando el receptor.

    @return True si el servicio del detector esta activo
    """
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet",
                            "guard-detector-rf"], timeout=3)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Barrido

def modo_barrido(args) -> int:
    """Genera el espectrograma de la banda a partir de barridos de
    hackrf_sweep.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    if receptor_ocupado():
        print("ERROR: el detector tiene el receptor. Pararlo antes:\n"
              "  sudo systemctl stop guard-detector-rf guard-panel",
              file=sys.stderr)
        return 1

    f_min, f_max = args.f_min, args.f_max
    n_col = int(round((f_max - f_min) * 1e6 / args.bin))

    print(f"Barriendo {f_min}-{f_max} MHz durante {args.segundos:.0f} s "
          f"(bin {args.bin / 1e3:.0f} kHz, {n_col} columnas)...")

    proc = subprocess.Popen(
        ["hackrf_sweep", "-f", f"{f_min}:{f_max}", "-w", str(int(args.bin))],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    filas = []
    fila = np.full(n_col, np.nan)
    vistos = set()
    t0 = time.monotonic()

    try:
        for linea in proc.stdout:
            if time.monotonic() - t0 > args.segundos:
                break
            c = linea.strip().split(", ")
            if len(c) < 7:
                continue
            try:
                hz_ini = int(c[2])
                paso = float(c[4])
                valores = [float(v) for v in c[6:]]
            except ValueError:
                continue

            # hackrf_sweep no entrega los tramos de un barrido en orden
            # —se vio 2410 antes que 2405— y sus marcas de tiempo no
            # separan barridos de forma fiable. Lo que si es fiable: un
            # tramo que ya se habia visto significa que empezo el barrido
            # siguiente.
            if hz_ini in vistos:
                filas.append(fila)
                fila = np.full(n_col, np.nan)
                vistos.clear()
            vistos.add(hz_ini)

            for k, v in enumerate(valores):
                col = int((hz_ini + k * paso - f_min * 1e6) / args.bin)
                if 0 <= col < n_col:
                    fila[col] = v
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not filas:
        print("ERROR: hackrf_sweep no entrego ningun barrido completo.",
              file=sys.stderr)
        return 1

    matriz = np.vstack(filas)
    # Huecos de algun tramo que no llego: se rellenan con el suelo de la
    # propia fila para que no aparezcan como franjas en la imagen.
    suelo = np.nanmedian(matriz)
    matriz = np.where(np.isnan(matriz), suelo, matriz)

    segundos_reales = time.monotonic() - t0
    por_fila_ms = 1000 * segundos_reales / len(filas)
    print(f"  {len(filas)} barridos, uno cada {por_fila_ms:.1f} ms")

    nombre = args.salida / f"barrido_{datetime.now():%Y%m%d-%H%M%S}"
    guardar(matriz, nombre,
            f"Barrido {f_min}-{f_max} MHz · {len(filas)} barridos · "
            f"{por_fila_ms:.0f} ms por fila",
            extent=(f_min, f_max, segundos_reales, 0),
            eje_y="tiempo (s)")
    return 0


# Iq

def calcular_iq(ruta: Path, nfft: int = 1024, filas_max: int = FILAS_IQ,
                ms: float = None, f_def: float = 2441e6,
                fs_def: float = 20e6) -> tuple:
    """Calcula el espectrograma de una captura .cs8. Lo usan la linea de
    ordenes y el panel.

    @param  ruta       fichero .cs8
    @param  nfft       puntos de la FFT
    @param  filas_max  numero maximo de filas de tiempo
    @param  ms         milisegundos a procesar (None para toda la captura)
    @param  f_def      frecuencia central si no hay metadatos, en Hz
    @param  fs_def     frecuencia de muestreo si no hay metadatos, en Hz
    @return tupla (matriz en dBFS, informacion para los ejes)
    """
    if np is None:
        raise RuntimeError("falta numpy: sudo apt install python3-numpy")

    meta = {}
    sidecar = ruta.with_suffix(".json")
    if sidecar.is_file():
        meta = json.loads(sidecar.read_text())
    fc = meta.get("f_centro_hz", f_def)
    fs = meta.get("muestreo_sps", fs_def)

    # memmap: el fichero no se carga entero. Un segundo son 40 MB en
    # crudo pero 160 MB como complejos de 32 bits, y la Pi tiene 2 GB.
    crudo = np.memmap(ruta, dtype=np.int8, mode="r")
    total = len(crudo) // 2
    if ms:
        total = min(total, int(fs * ms / 1000))

    n_tramas = total // nfft
    if n_tramas == 0:
        raise RuntimeError("captura demasiado corta para esa FFT")

    por_fila = max(1, n_tramas // filas_max)
    n_filas = n_tramas // por_fila
    ventana = np.hanning(nfft).astype(np.float32)
    # Normalizacion: un tono a fondo de escala sale a 0 dBFS.
    escala = 1.0 / (np.sum(ventana) * 128.0)

    matriz = np.empty((n_filas, nfft), dtype=np.float32)
    for i in range(n_filas):
        ini = i * por_fila * nfft * 2
        fin = ini + por_fila * nfft * 2
        b = np.asarray(crudo[ini:fin], dtype=np.float32)
        iq = (b[0::2] + 1j * b[1::2]).reshape(por_fila, nfft)
        espectro = np.fft.fftshift(np.fft.fft(iq * ventana, axis=1), axes=1)
        potencia = np.abs(espectro * escala) ** 2
        # Maximo, no media, al agrupar tramas: una rafaga corta de
        # Bluetooth ocupa una sola trama de las varias que se juntan, y
        # promediarla la diluiria hasta hacerla invisible. Es la misma
        # decision que en el panel y en el detector.
        matriz[i] = 10 * np.log10(potencia.max(axis=0) + 1e-12)

    info = {
        "fc_hz": fc,
        "fs_sps": fs,
        "f_lo_mhz": (fc - fs / 2) / 1e6,
        "f_hi_mhz": (fc + fs / 2) / 1e6,
        "duracion_ms": n_filas * por_fila * nfft / fs * 1000,
        "etiqueta": meta.get("etiqueta", "captura"),
        "n_tramas": n_tramas,
        "por_fila": por_fila,
        "nfft": nfft,
    }
    return matriz, info


def modo_iq(args) -> int:
    """Genera el espectrograma de una captura IQ y lo guarda.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    ruta = Path(args.fichero)
    if not ruta.is_file():
        print(f"ERROR: no existe {ruta}", file=sys.stderr)
        return 1

    try:
        matriz, info = calcular_iq(ruta, nfft=args.nfft, ms=args.ms,
                                   f_def=args.f * 1e6, fs_def=args.muestreo)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    fs, nfft = info["fs_sps"], info["nfft"]
    print(f"{ruta.name}: {info['duracion_ms']:.1f} ms · "
          f"FFT {nfft} ({fs / nfft / 1e3:.1f} kHz por bin, "
          f"{nfft / fs * 1e6:.1f} us por trama)")
    print(f"  {info['n_tramas']} tramas -> {matriz.shape[0]} filas "
          f"({info['por_fila']} tramas por fila)")

    nombre = args.salida / ruta.stem
    guardar(matriz, nombre,
            f"{info['etiqueta']} · {info['fc_hz'] / 1e6:.0f} MHz ± "
            f"{fs / 2e6:.0f} · {info['duracion_ms']:.0f} ms",
            extent=(info["f_lo_mhz"], info["f_hi_mhz"],
                    info["duracion_ms"], 0),
            eje_y="tiempo (ms)")
    return 0


# Main

def main() -> int:
    """Lee los argumentos y ejecuta el modo elegido (barrido o iq).

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Espectrogramas de banda o de IQ")
    sub = p.add_subparsers(dest="modo", required=True)

    b = sub.add_parser("barrido", help="banda ancha con hackrf_sweep")
    b.add_argument("--f-min", type=int, default=2400)
    b.add_argument("--f-max", type=int, default=2500)
    b.add_argument("--bin", type=float, default=500_000,
                   help="ancho de bin en Hz (def. 500 kHz)")
    b.add_argument("--segundos", type=float, default=10.0)
    b.add_argument("--salida", type=Path, default=SALIDA_DEF)

    q = sub.add_parser("iq", help="espectrograma fino de una captura .cs8")
    q.add_argument("fichero")
    q.add_argument("--nfft", type=int, default=1024,
                   help="tamano de FFT (def. 1024: ~20 kHz y ~51 us a 20 Msps)")
    q.add_argument("--ms", type=float, default=None,
                   help="usar solo los primeros N milisegundos")
    q.add_argument("--f", type=float, default=2441,
                   help="frecuencia central si no hay .json")
    q.add_argument("--muestreo", type=float, default=20e6,
                   help="muestreo si no hay .json")
    q.add_argument("--salida", type=Path, default=SALIDA_DEF)

    args = p.parse_args()
    if np is None:
        print("ERROR: falta numpy.  sudo apt install python3-numpy",
              file=sys.stderr)
        return 1
    return modo_barrido(args) if args.modo == "barrido" else modo_iq(args)


if __name__ == "__main__":
    sys.exit(main())
