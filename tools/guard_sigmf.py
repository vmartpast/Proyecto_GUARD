#!/usr/bin/env python3
"""guard_sigmf: importa grabaciones SigMF externas al dataset de GUARD,
convirtiendolas al formato del HackRF (20 Msps, 8 bits).
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

VERSION = "1.0"

FS_SALIDA = 20_000_000          # muestreo del dataset propio (HackRF)
N_TAPS = 65                     # orden del FIR; N_TAPS-1 multiplo del diezmado
TROZO = 4_000_000               # muestras de entrada por bloque (~32 MB cf32)
OBJETIVO = 100                  # amplitud tipica maxima tras escalar (de 127)

# Tipos SigMF admitidos -> (dtype numpy de cada componente, bytes por muestra)
TIPOS = {
    "cf32_le": (np.dtype("<f4"), 8),
    "ci16_le": (np.dtype("<i2"), 4),
    "ci8": (np.dtype("i1"), 2),
}

# Etiquetas que se proponen para el origen externo. No restringen.
CLASES_EXTERNAS = {
    "uav_mini3": "DJI Mini 3",
    "uav_mavic_pro": "DJI Mavic Pro",
    "uav_hunter": "Hunter",
    "uav_f450": "F450 (montaje propio)",
    "sin_uav": "mismo entorno y receptor que los UAV, sin dron",
}


# Utilidades

def bytes_legibles(n: float) -> str:
    """Convierte un numero de bytes en texto legible (KB, MB, GB...).

    @param  n  numero de bytes
    @return texto con una cifra decimal y su unidad, p. ej. "1.8 GB"
    """
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unidad}"
        n /= 1024
    return f"{n:.1f} PB"


def sha256(ruta: Path) -> str:
    """Calcula la huella SHA-256 de un fichero, leyendolo en bloques de 1 MB.

    @param  ruta  fichero del que calcular la huella
    @return huella en hexadecimal (64 caracteres)
    """
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for trozo in iter(lambda: f.read(1024 * 1024), b""):
            h.update(trozo)
    return h.hexdigest()


def rutas_sigmf(ruta: Path):
    """Obtiene las rutas de los dos ficheros de una grabacion SigMF.

    @param  ruta  ruta al .sigmf-meta o al .sigmf-data
    @return tupla (ruta_meta, ruta_data)
    """
    nombre = ruta.name
    for suf in (".sigmf-meta", ".sigmf-data"):
        if nombre.endswith(suf):
            base = nombre[: -len(suf)]
            return (ruta.with_name(base + ".sigmf-meta"),
                    ruta.with_name(base + ".sigmf-data"))
    raise ValueError(f"no parece SigMF: {ruta}")


def leer_meta(meta: Path) -> dict:
    """Lee el .sigmf-meta y extrae los campos necesarios para convertir.

    @param  meta  ruta al fichero .sigmf-meta
    @return diccionario con tipo de muestra, frecuencia de muestreo, frecuencia de sintonia, autor y version del estandar
    """
    with open(meta) as f:
        m = json.load(f)
    g = m.get("global", {})
    caps = m.get("captures", [{}])
    return {
        "tipo": g.get("core:datatype"),
        "fs": float(g.get("core:sample_rate", 0)),
        "f_centro": float(caps[0].get("core:frequency", 0)),
        "autor": g.get("core:author", ""),
        "version_sigmf": g.get("core:version", ""),
    }


def campos_del_nombre(base: str) -> dict:
    """Extrae del nombre del fichero lo que este dice de la grabacion.

    @param  base  nombre del fichero sin extension
    @return diccionario solo con los campos encontrados: distancia, wifi_origen, sesion_origen, con_wifi, sin_dron
    """
    c = {}
    if m := re.search(r"_d(\d+)_", base):
        c["distancia"] = int(m.group(1))
    if m := re.search(r"_(w\d+)_", base):
        c["wifi_origen"] = m.group(1)
    if m := re.search(r"_(s\d+)_", base):
        c["sesion_origen"] = m.group(1)
    if "conwifi" in base:
        c["con_wifi"] = True
    if "nodrone" in base:
        c["sin_dron"] = True
    return c


def disenar_fir(n: int, corte: float) -> np.ndarray:
    """Disena un filtro FIR paso bajo por el metodo de la ventana.

    @param  n      numero de coeficientes (impar, 65 aqui)
    @param  corte  frecuencia de corte en ciclos por muestra (0 a 0,5)
    @return coeficientes del filtro, array float32 de longitud n
    """
    k = np.arange(n) - (n - 1) / 2
    h = 2 * corte * np.sinc(2 * corte * k) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


class Fuente:
    """Fichero .sigmf-data, o sus piezas .p0, .p1... si se descargo troceado."""

    def __init__(self, data: Path):
        """Localiza el fichero completo o, si no existe, sus piezas consecutivas.

        @param  data  ruta esperada del .sigmf-data
        """
        if data.is_file():
            self.partes = [data]
        else:
            self.partes = []
            i = 0
            while (q := data.with_name(f"{data.name}.p{i}")).is_file():
                self.partes.append(q)
                i += 1
        self.tamanos = [q.stat().st_size for q in self.partes]
        self.total = sum(self.tamanos)

    def existe(self) -> bool:
        """Indica si hay algo que leer.

        @return True si se encontro el fichero o al menos una pieza
        """
        return bool(self.partes)

    def leer(self, inicio_b: int, n_b: int) -> bytes:
        """Lee bytes a partir de una posicion, como si las piezas fueran un
        fichero.

        @param  inicio_b  posicion de inicio en bytes desde el principio
        @param  n_b       numero de bytes a leer
        @return bytes leidos (menos de n_b solo si se llega al final)
        """
        trozos, pos = [], 0
        for q, t in zip(self.partes, self.tamanos):
            if n_b <= 0:
                break
            if inicio_b < pos + t:
                desde = max(0, inicio_b - pos)
                k = min(t - desde, n_b)
                with open(q, "rb") as f:
                    f.seek(desde)
                    trozos.append(f.read(k))
                inicio_b += k
                n_b -= k
            pos += t
        return b"".join(trozos)

    def borrar(self):
        """Borra el fichero o todas sus piezas. Se llama tras convertir con
        exito.
        """
        for q in self.partes:
            q.unlink()


def leer_bloque(fuente: "Fuente", inicio: int, cuenta: int, tipo) -> np.ndarray:
    """Lee un bloque de muestras IQ y lo devuelve como numeros complejos.

    @param  fuente  Fuente de la que leer
    @param  inicio  primera muestra (en muestras, no en bytes)
    @param  cuenta  numero de muestras complejas a leer
    @param  tipo    formato SigMF ("cf32_le", "ci16_le" o "ci8")
    @return array complex64 con I + jQ
    """
    dt, bps = TIPOS[tipo]
    crudo = np.frombuffer(fuente.leer(inicio * bps, cuenta * bps), dtype=dt)
    crudo = crudo.astype(np.float32)
    return crudo[0::2] + 1j * crudo[1::2]


# Conversion

def estimar_escala(fuente, total: int, tipo, h, d) -> tuple:
    """Calcula el factor de escala para cuantizar a 8 bits sin saturar.

    @param  fuente  Fuente de la grabacion original
    @param  total   numero de muestras que se van a convertir
    @param  tipo    formato SigMF de las muestras
    @param  h       coeficientes del filtro FIR
    @param  d       factor de diezmado (2 para 40 -> 20 Msps)
    @return tupla (escala, pico): factor multiplicativo y pico medido
    """
    muestras = []
    n = 500_000
    for pos in np.linspace(0, max(total - n, 0), 24).astype(np.int64):
        x = leer_bloque(fuente, int(pos), n, tipo)
        if x.size < len(h):
            continue
        y = np.convolve(x, h, mode="valid")[::d]
        muestras.append(np.maximum(np.abs(y.real), np.abs(y.imag)))
    v = np.concatenate(muestras)
    pico = float(np.percentile(v, 99.99))
    return (OBJETIVO / pico if pico > 0 else 1.0), pico


def convertir(args) -> int:
    """Convierte una grabacion SigMF al formato del dataset propio (.cs8).

    @param  args  argumentos de linea de ordenes: fichero, etiqueta, salida, dron, nota, segundos, borrar_origen, sin_hash
    @return 0 si todo fue bien, 1 si hubo error
    """
    meta, data = rutas_sigmf(Path(args.fichero))
    fuente = Fuente(data)
    if not meta.is_file() or not fuente.existe():
        print(f"error: faltan {meta.name} o {data.name}", file=sys.stderr)
        return 1

    m = leer_meta(meta)
    if m["tipo"] not in TIPOS:
        print(f"error: tipo {m['tipo']} no soportado "
              f"(admitidos: {', '.join(TIPOS)})", file=sys.stderr)
        return 1
    if m["fs"] % FS_SALIDA:
        print(f"error: {m['fs']/1e6:g} Msps no es multiplo de "
              f"{FS_SALIDA/1e6:g} Msps", file=sys.stderr)
        return 1

    d = int(m["fs"] // FS_SALIDA)
    bps = TIPOS[m["tipo"]][1]
    total = fuente.total // bps
    if args.segundos:
        total = min(total, int(args.segundos * m["fs"]))
    total -= total % d
    n_salida = total // d

    raiz = Path(args.salida)
    carpeta = raiz / args.etiqueta
    carpeta.mkdir(parents=True, exist_ok=True)
    tamano = n_salida * 2
    libre = shutil.disk_usage(raiz).free
    if libre < tamano * 1.5:
        print(f"error: quedan {bytes_legibles(libre)} y la salida ocupa "
              f"{bytes_legibles(tamano)}", file=sys.stderr)
        return 1

    base = data.name[: -len(".sigmf-data")]
    destino = carpeta / f"{args.etiqueta}_{base}.cs8"
    parcial = destino.with_suffix(".cs8.parcial")

    # Corte al 90 % de la nueva Nyquist: la banda util se conserva y la
    # zona de transicion cae fuera de ella.
    h = disenar_fir(N_TAPS, 0.9 * (FS_SALIDA / 2) / m["fs"])
    cola = N_TAPS - 1
    assert cola % d == 0

    escala, pico = estimar_escala(fuente, total, m["tipo"], h, d)

    print(f"{base}: {m['tipo']} {m['fs']/1e6:g} Msps @ "
          f"{m['f_centro']/1e6:g} MHz, {total/m['fs']:.2f} s "
          f"-> {FS_SALIDA/1e6:g} Msps cs8 ({bytes_legibles(tamano)})")

    t0 = time.monotonic()
    anterior = np.zeros(cola, dtype=np.complex64)
    recortadas = 0
    escritas = 0
    with open(parcial, "wb") as f:
        pos = 0
        while pos < total:
            n = min(TROZO, total - pos)
            x = leer_bloque(fuente, pos, n, m["tipo"])
            if x.size != n:
                print(f"\nerror: lectura corta en {pos}", file=sys.stderr)
                parcial.unlink(missing_ok=True)
                return 1
            buf = np.concatenate([anterior, x])
            # La salida k de 'valid' corresponde a la muestra global pos+k;
            # pos es multiplo de d, asi que basta con tomar k = 0, d, 2d...
            y = np.convolve(buf, h, mode="valid")[::d] * escala
            anterior = buf[-cola:]

            re_ = np.round(y.real)
            im_ = np.round(y.imag)
            recortadas += int(np.count_nonzero(np.abs(re_) > 127)
                              + np.count_nonzero(np.abs(im_) > 127))
            sal = np.empty(2 * y.size, dtype=np.int8)
            sal[0::2] = np.clip(re_, -127, 127)
            sal[1::2] = np.clip(im_, -127, 127)
            sal.tofile(f)
            escritas += y.size

            pos += n
            print(f"\r  {100*pos/total:5.1f} %", end="", flush=True)
    print()

    if escritas != n_salida:
        parcial.unlink(missing_ok=True)
        print(f"error: {escritas} muestras escritas, se esperaban "
              f"{n_salida}", file=sys.stderr)
        return 1
    parcial.replace(destino)
    transcurrido = time.monotonic() - t0

    registro = {
        "fichero": str(destino.relative_to(raiz)),
        "etiqueta": args.etiqueta,
        "nota": args.nota,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "formato": "cs8",
        "formato_detalle": ("entero de 8 bits con signo, I y Q "
                            "intercalados: I,Q,I,Q..."),
        "f_centro_hz": int(m["f_centro"]),
        "muestreo_sps": FS_SALIDA,
        "ancho_banda_hz": FS_SALIDA,
        "duracion_s": round(n_salida / FS_SALIDA, 4),
        "n_muestras": n_salida,
        "bytes": destino.stat().st_size,
        "receptor": "USRP" if "usrp" in m["autor"].lower() else m["autor"],
        "origen": "externo",
        "origen_fichero": data.name,
        "origen_bytes_usados": fuente.total,
        "origen_tipo": m["tipo"],
        "origen_muestreo_sps": int(m["fs"]),
        "conversion": {
            "diezmado": d,
            "fir_taps": N_TAPS,
            "fir_corte_hz": 0.9 * FS_SALIDA / 2,
            "escala": escala,
            "pico_robusto_origen": pico,
            "fraccion_recortada": recortadas / max(2 * escritas, 1),
            "dc_corregida": False,
        },
        "herramienta": f"guard_sigmf {VERSION}",
        "segundos_reales": round(transcurrido, 1),
    }
    registro.update(campos_del_nombre(base))
    if args.dron:
        registro["dron"] = args.dron
    if not args.sin_hash:
        registro["sha256"] = sha256(destino)

    with open(destino.with_suffix(".json"), "w") as f:
        json.dump(registro, f, indent=2, ensure_ascii=False)
    with open(raiz / "manifest.jsonl", "a") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    frac = registro["conversion"]["fraccion_recortada"]
    print(f"  ok: {destino.name} en {transcurrido:.0f} s, "
          f"recortado {100*frac:.4f} %")
    if frac > 1e-3:
        print("  aviso: mas del 0,1 % de muestras recortadas")

    if args.borrar_origen:
        try:
            fuente.borrar()
            print(f"  borrado el original {data.name}"
                  + (f" ({len(fuente.partes)} piezas)"
                     if len(fuente.partes) > 1 else ""))
        except OSError as e:
            print(f"  aviso: no se pudo borrar el original: {e}")
    return 0


def info(args) -> int:
    """Muestra los metadatos de una grabacion SigMF sin convertirla.

    @param  args  argumentos de linea de ordenes (solo usa args.fichero)
    @return 0
    """
    meta, data = rutas_sigmf(Path(args.fichero))
    m = leer_meta(meta)
    print(json.dumps(m, indent=2))
    if data.is_file() and m["tipo"] in TIPOS:
        n = data.stat().st_size // TIPOS[m["tipo"]][1]
        print(f"duracion: {n / m['fs']:.2f} s "
              f"({bytes_legibles(data.stat().st_size)})")
    print("del nombre:", campos_del_nombre(meta.name[: -len('.sigmf-meta')]))
    return 0


def main() -> int:
    """Punto de entrada: interpreta la linea de ordenes y llama al modo
    pedido.

    @return codigo de salida para el sistema operativo (0 = correcto)
    """
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = p.add_subparsers(dest="modo", required=True)

    pi = sub.add_parser("info", help="muestra los metadatos")
    pi.add_argument("fichero")

    pc = sub.add_parser("convertir", help="convierte a cs8 @ 20 Msps")
    pc.add_argument("fichero")
    pc.add_argument("--etiqueta", required=True,
                    help="propuestas: " + ", ".join(CLASES_EXTERNAS))
    pc.add_argument("--salida", required=True)
    pc.add_argument("--dron", default="")
    pc.add_argument("--nota", default="")
    pc.add_argument("--segundos", type=float, default=0,
                    help="convertir solo los primeros N segundos")
    pc.add_argument("--borrar-origen", action="store_true",
                    help="borra el .sigmf-data si la conversion termina bien")
    pc.add_argument("--sin-hash", action="store_true")

    args = p.parse_args()
    return info(args) if args.modo == "info" else convertir(args)


if __name__ == "__main__":
    sys.exit(main())
