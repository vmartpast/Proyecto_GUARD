#!/usr/bin/env python3
"""guard_captura: adquisicion del conjunto de datos. Graba capturas IQ con el
HackRF (.cs8), su fichero de metadatos (.json) y el indice manifest.jsonl.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0"

# Centro y ancho por defecto. 2441 MHz con 20 Msps cubre 2431-2451, la
# zona mas poblada de la banda: canales WiFi 6 a 9 y buena parte del
# salto del Bluetooth.
F_CENTRO_MHZ = 2441
MUESTREO_SPS = 20_000_000
DURACION_S = 1.0

# Ganancias. Hay dos riesgos opuestos. Con demasiada ganancia la senal se
# recorta, y un tramo saturado tiene armonicos e intermodulacion que no
# existen en el aire. Con demasiado poca, el ruido de cuantizacion tapa las
# senales debiles: con LNA 16 / VGA 20 el ambiente solo tomaba 6 valores
# distintos de 256 y los rasgos cambiaban por completo.
# Medido (21 oct.) con un movil subiendo por WiFi a 1 m: LNA 32 / VGA 32
# da un maximo de 52 de 127, 98 valores distintos y nada saturado. Es la
# configuracion con la que se entreno el clasificador, y tiene que ser la
# misma al operar: la ganancia forma parte del modelo.
LNA_DB = 32     # 0-40, pasos de 8
VGA_DB = 32     # 0-62, pasos de 2
AMP = 0         # amplificador externo: apagado

RAIZ_DEF = Path("/var/lib/guard/dataset")

# Margen de disco exigido antes de capturar, como multiplo del tamano de
# la captura. Este equipo ya se ha quedado sin espacio una vez.
MARGEN_DISCO = 3.0

# Clases previstas. No es una restriccion —se acepta cualquier etiqueta—
# pero tenerlas escritas evita que la misma condicion acabe guardada con
# tres nombres distintos a lo largo de una campana.
CLASES = {
    # 'ambiente' y no 'ruido': en un edificio nunca hay silencio, siempre
    # quedan los emisores de los vecinos. Lo que esta clase recoge es el
    # fondo que el detector vera siempre, con nada propio encendido.
    "ambiente": "nada propio activo: el fondo del entorno. La clase negativa",
    "wifi_inactivo": "punto de acceso presente, sin trafico",
    "wifi_trafico": "punto de acceso con trafico saturando",
    "bluetooth_busqueda": "emparejamiento o escaneo activo",
    "bluetooth_audio": "audio en curso por Bluetooth",
    "mixto": "WiFi y Bluetooth simultaneos",
    "sin_etiquetar": "capturado desde el panel; pendiente de clasificar",
    "uav_control": "enlace de control de UAV",
    "uav_video": "enlace de video de UAV",
}

_parar = False


def _stop(signum, frame):
    """Detiene la serie de capturas al recibir SIGTERM o SIGINT.

    @param  signum  senal recibida
    @param  frame   marco de pila actual (no se usa)
    """
    global _parar
    _parar = True


# Utilidades

def ahora_utc() -> datetime:
    """Devuelve la fecha y hora actuales en UTC.

    @return marca de tiempo en formato ISO 8601
    """
    return datetime.now(timezone.utc)


def serial_hackrf() -> str:
    """Obtiene el numero de serie del HackRF para registrarlo en los
    metadatos.

    @return numero de serie, o 'desconocido'
    """
    try:
        r = subprocess.run(["hackrf_info"], capture_output=True, text=True,
                           timeout=5)
        for linea in r.stdout.splitlines():
            if "Serial number" in linea:
                return linea.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "desconocido"


def receptor_ocupado() -> str:
    """Comprueba si algun proceso o servicio esta usando el HackRF.

    @return descripcion de quien lo usa, o cadena vacia si esta libre
    """
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet",
                            "guard-detector-rf"], timeout=3)
        if r.returncode == 0:
            return "guard-detector-rf.service"
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        r = subprocess.run(["pgrep", "-a", "hackrf_"], capture_output=True,
                           text=True, timeout=3)
        if r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass

    return ""


def bytes_legibles(n: float) -> str:
    """Convierte un numero de bytes en texto legible (KB, MB, GB).

    @param  n  numero de bytes
    @return texto con la cifra y su unidad
    """
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unidad}"
        n /= 1024
    return f"{n:.1f} PB"


def sha256(ruta: Path) -> str:
    """Calcula la huella SHA-256 de un fichero.

    @param  ruta  fichero del que calcular la huella
    @return huella en hexadecimal
    """
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for trozo in iter(lambda: f.read(1024 * 1024), b""):
            h.update(trozo)
    return h.hexdigest()


# Captura

def una_captura(args, raiz: Path, serie: str, indice: int) -> dict:
    """Graba una captura IQ con hackrf_transfer y escribe sus metadatos.

    @param  args    argumentos de la linea de ordenes
    @param  raiz    carpeta raiz del dataset
    @param  serie   numero de serie del receptor
    @param  indice  numero de la captura dentro de la serie
    @return registro de la captura para el manifiesto
    """
    muestras = int(args.muestreo * args.duracion)
    tamano = muestras * 2          # un byte para I y otro para Q

    libre = shutil.disk_usage(raiz).free
    if libre < tamano * MARGEN_DISCO:
        raise RuntimeError(
            f"disco insuficiente: quedan {bytes_legibles(libre)} y la "
            f"captura ocupa {bytes_legibles(tamano)}; se exige "
            f"{MARGEN_DISCO:g}x de margen")

    carpeta = raiz / args.etiqueta
    carpeta.mkdir(parents=True, exist_ok=True)

    t0 = ahora_utc()
    base = f"{args.etiqueta}_{t0.strftime('%Y%m%d-%H%M%S')}"
    destino = carpeta / f"{base}.cs8"

    cmd = [
        "hackrf_transfer",
        "-r", str(destino),
        "-f", str(int(args.f * 1e6)),
        "-s", str(int(args.muestreo)),
        "-n", str(muestras),
        "-l", str(args.lna),
        "-g", str(args.vga),
        "-a", str(args.amp),
    ]

    print(f"  [{indice}/{args.n}] {base}  "
          f"{args.duracion:.1f} s · {bytes_legibles(tamano)} ... ",
          end="", flush=True)

    inicio = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=args.duracion + 30)
    transcurrido = time.monotonic() - inicio

    real = destino.stat().st_size if destino.is_file() else 0
    # Una captura corta es una captura invalida: hackrf_transfer no
    # siempre devuelve codigo de error cuando el streaming se interrumpe,
    # asi que el tamano es la comprobacion que de verdad vale.
    if real < tamano:
        destino.unlink(missing_ok=True)
        raise RuntimeError(
            f"captura incompleta: {bytes_legibles(real)} de "
            f"{bytes_legibles(tamano)}. Salida de hackrf_transfer:\n"
            f"{r.stderr.strip()[-400:]}")

    registro = {
        "fichero": str(destino.relative_to(raiz)),
        "etiqueta": args.etiqueta,
        "nota": args.nota,
        "ts_utc": t0.isoformat(),
        "ts_local": t0.astimezone().isoformat(),
        "formato": "cs8",
        "formato_detalle": ("entero de 8 bits con signo, I y Q "
                            "intercalados: I,Q,I,Q..."),
        "f_centro_hz": int(args.f * 1e6),
        "muestreo_sps": int(args.muestreo),
        "ancho_banda_hz": int(args.muestreo),
        "duracion_s": args.duracion,
        "n_muestras": muestras,
        "bytes": real,
        "ganancia_lna_db": args.lna,
        "ganancia_vga_db": args.vga,
        "amplificador": args.amp,
        "receptor": "HackRF One",
        "serie_receptor": serie,
        "herramienta": f"guard_captura {VERSION}",
        "segundos_reales": round(transcurrido, 2),
    }
    if not args.sin_hash:
        registro["sha256"] = sha256(destino)

    with open(destino.with_suffix(".json"), "w") as f:
        json.dump(registro, f, indent=2, ensure_ascii=False)

    with open(raiz / "manifest.jsonl", "a") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    print(f"ok ({transcurrido:.1f} s)")
    return registro


def modo_captura(args) -> int:
    """Graba la serie de capturas pedida y las anade al manifiesto.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    ocupa = receptor_ocupado()
    if ocupa:
        print(f"ERROR: el receptor lo tiene {ocupa}.", file=sys.stderr)
        print("Pararlo antes de capturar:\n"
              "  sudo systemctl stop guard-detector-rf guard-panel",
              file=sys.stderr)
        return 1

    raiz = args.raiz
    try:
        raiz.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: no se pudo crear {raiz}: {exc}", file=sys.stderr)
        return 1

    if args.etiqueta not in CLASES:
        print(f"Aviso: '{args.etiqueta}' no esta en las clases previstas.")
        print("Previstas: " + ", ".join(CLASES))
        print()

    serie = serial_hackrf()
    total = int(args.muestreo * args.duracion) * 2 * args.n

    print(f"Dataset   {raiz}")
    print(f"Clase     {args.etiqueta}"
          + (f"   «{args.nota}»" if args.nota else ""))
    print(f"Receptor  HackRF One {serie}")
    print(f"Sintonia  {args.f} MHz · {args.muestreo / 1e6:.0f} Msps · "
          f"LNA {args.lna} dB · VGA {args.vga} dB")
    print(f"Plan      {args.n} capturas de {args.duracion:.1f} s "
          f"= {bytes_legibles(total)}")
    print()

    hechas = 0
    for i in range(1, args.n + 1):
        if _parar:
            print("\n  interrumpido")
            break
        try:
            una_captura(args, raiz, serie, i)
            hechas += 1
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print("FALLO")
            print(f"      {exc}", file=sys.stderr)
            # Una captura fallida no aborta la campana: puede ser un
            # tropiezo puntual del USB. Pero si falla la primera, algo
            # esta mal de verdad y no merece la pena insistir.
            if i == 1:
                return 1
        if i < args.n and not _parar:
            time.sleep(args.pausa)

    print()
    print(f"{hechas} de {args.n} capturas guardadas en {raiz / args.etiqueta}")
    return 0 if hechas else 1


# Listado

def modo_listar(args) -> int:
    """Muestra un resumen del dataset a partir del manifiesto.

    @param  args  argumentos de la linea de ordenes
    @return codigo de salida
    """
    raiz = args.raiz
    manifiesto = raiz / "manifest.jsonl"
    if not manifiesto.is_file():
        print(f"No hay dataset en {raiz}.")
        return 0

    registros = []
    with open(manifiesto) as f:
        for linea in f:
            try:
                registros.append(json.loads(linea))
            except json.JSONDecodeError:
                continue

    if not registros:
        print("El manifiesto esta vacio.")
        return 0

    por_clase = {}
    for r in registros:
        c = por_clase.setdefault(r["etiqueta"], {"n": 0, "bytes": 0, "s": 0.0})
        c["n"] += 1
        c["bytes"] += r.get("bytes", 0)
        c["s"] += r.get("duracion_s", 0.0)

    print(f"Dataset en {raiz}\n")
    print(f"  {'clase':<22}{'capturas':>9}{'segundos':>10}{'tamano':>12}")
    print("  " + "-" * 53)
    for clase in sorted(por_clase):
        c = por_clase[clase]
        print(f"  {clase:<22}{c['n']:>9}{c['s']:>9.0f}s"
              f"{bytes_legibles(c['bytes']):>12}")
    print("  " + "-" * 53)
    tot_n = sum(c["n"] for c in por_clase.values())
    tot_s = sum(c["s"] for c in por_clase.values())
    tot_b = sum(c["bytes"] for c in por_clase.values())
    print(f"  {'TOTAL':<22}{tot_n:>9}{tot_s:>9.0f}s"
          f"{bytes_legibles(tot_b):>12}")

    # El desequilibrio entre clases es el problema que mas estropea un
    # clasificador y el mas facil de no ver, asi que se avisa aqui.
    if len(por_clase) > 1:
        mn = min(c["n"] for c in por_clase.values())
        mx = max(c["n"] for c in por_clase.values())
        if mx > 2 * mn:
            print(f"\n  Aviso: clases desequilibradas ({mn} frente a {mx}).")
            print("  Conviene igualarlas antes de entrenar.")

    libre = shutil.disk_usage(raiz).free
    print(f"\n  Disco libre: {bytes_legibles(libre)}")
    return 0


# Main

def main() -> int:
    """Lee los argumentos y ejecuta el modo elegido (capturar o listar).

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(
        description="Adquisicion de dataset de IQ etiquetado",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Clases previstas:\n" + "\n".join(
            f"  {k:<22}{v}" for k, v in CLASES.items()))

    p.add_argument("--etiqueta", help="clase de la captura")
    p.add_argument("--nota", default="",
                   help="que estaba activo y en que condiciones. Sin esto "
                        "la etiqueta no se puede auditar despues")
    p.add_argument("--n", type=int, default=10, help="numero de capturas")
    p.add_argument("--f", type=float, default=F_CENTRO_MHZ,
                   help=f"frecuencia central en MHz (def. {F_CENTRO_MHZ})")
    p.add_argument("--muestreo", type=float, default=MUESTREO_SPS,
                   help="muestras por segundo")
    p.add_argument("--duracion", type=float, default=DURACION_S,
                   help="segundos por captura")
    p.add_argument("--lna", type=int, default=LNA_DB,
                   help="ganancia de LNA en dB, 0-40 en pasos de 8")
    p.add_argument("--vga", type=int, default=VGA_DB,
                   help="ganancia de VGA en dB, 0-62 en pasos de 2")
    p.add_argument("--amp", type=int, default=AMP,
                   help="amplificador externo, 0 o 1")
    p.add_argument("--pausa", type=float, default=1.0,
                   help="segundos entre capturas")
    p.add_argument("--raiz", type=Path, default=RAIZ_DEF,
                   help=f"raiz del dataset (def. {RAIZ_DEF})")
    p.add_argument("--sin-hash", action="store_true",
                   help="no calcular el sha256 de cada captura")
    p.add_argument("--listar", action="store_true",
                   help="resumen del dataset y salir")
    args = p.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if args.listar:
        return modo_listar(args)

    if not args.etiqueta:
        p.error("hace falta --etiqueta (o --listar)")

    return modo_captura(args)


if __name__ == "__main__":
    sys.exit(main())
