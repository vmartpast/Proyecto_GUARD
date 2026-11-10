#!/usr/bin/env python3
"""guard_panel: panel de operador en el monitor HDMI, navegado con el joystick.
Muestra el estado, el espectro y su cascada, las emisiones, la lista blanca,
la salud del sistema y el registro, y permite pedir una captura fina.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from guard_rf import (
        capturar, analizar, identificar, marcar_conocidos,
        cargar_lista_blanca, CONFIG_DEF, SNAPSHOT_FILE,
    )
except ImportError as exc:
    print(f"ERROR: guard_rf.py debe estar en el mismo directorio: {exc}",
          file=sys.stderr)
    sys.exit(1)

# El perfil del espectro se pinta con la misma escala de caracteres y
# colores que la cascada (░▒▓█). Antes usaba bloques de altura creciente
# (▁▂▃▄▅▆▇), pero la fuente de la consola de la Raspberry no los tiene y
# salian como "#". Los de la cascada estan en cualquier fuente de consola.

# Escala de la cascada. Los cortes estan en dB sobre el suelo de ruido, no
# en potencia absoluta: es lo unico que determina la detectabilidad, y hace
# que la cascada siga siendo legible cuando el suelo cambia.
CASCADA_NIVELES = [
    (20.0, "█", "rojo"),
    (14.0, "▓", "amarillo"),
    (8.0,  "▒", "azul"),
    # Por debajo de 4 dB no hay nada que mirar: son las fluctuaciones
    # propias del suelo de ruido, y pintarlas llena la cascada de mota
    # que esconde lo que si importa.
    (4.0,  "░", "gris"),
    (0.0,  " ", "gris"),
]

CASCADA_MAX = 24
REGISTRO_MAX = 60

# Un snapshot mas antiguo que esto significa que el detector no esta
# publicando: se muestra el dato con aviso en lugar de fingir que es actual.
ANTIGUEDAD_MAX_S = 30.0

# La salud de la plataforma cambia despacio y su lectura cuesta un proceso
# externo. Se refresca cada pocos segundos, no en cada pintado.
SALUD_TTL_S = 10.0

# Pulsaciones publicadas por el puente.
ENTRADA_FILE = Path("/run/guard/input.json")

# Cadencias. La entrada se mira mucho mas a menudo que el analisis: un
# menu que responde cada segundo y medio se siente roto.
PERIODO_ENTRADA_S = 0.1
PERIODO_DATOS_S = 1.5

ANCHO_MENU = 18
# Columnas que el espectro deja libres a su derecha para la etiqueta
# «ahora» / «max 15 s»; sin ellas la linea empujaria al menu.
ANCHO_ETIQUETA = 15

# Captura fina pedida desde el panel. La graba el detector, que es el unico
# dueno del receptor; el panel solo deja la peticion y espera la respuesta.
PETICION_FILE = Path("/run/guard/peticion.json")
RESPUESTA_FILE = Path("/run/guard/respuesta.json")
CAPTURA_MS = 50.0          # lo que se graba y se guarda en el dataset
CAPTURA_VER_MS = 25.0      # lo que se dibuja: en 25 ms ya caben 40 saltos
CAPTURA_TIMEOUT_S = 20.0

# Resultado del clasificador de UAV (guard_clasificador). Un resultado
# mas viejo que esto ya no describe lo que hay en el aire: se muestra en
# gris y no dispara el estado de alerta.
CLASIF_FILE = Path("/run/guard/clasificacion.json")
CLASIF_VIGENTE_S = 60.0
# Un "posible UAV" se muestra mientras puede llegar su confirmacion (el
# clasificador la espera 30 s); despues ya no dice nada actual.
CONFIRMA_VIGENTE_S = 30.0
NOMBRE_CLASE = {
    "sin_uav": "sin UAV",
    "uav_mini3": "DJI Mini 3",
    "uav_mavic_pro": "DJI Mavic Pro",
    "uav_hunter": "Hunter",
    "uav_f450": "F450",
}

# Orden del menu. La primera es la vista de arranque y la de inicio.
VISTAS = [
    ("ESTADO", "estado"),
    ("ESPECTRO", "espectro"),
    ("EMISIONES", "emisiones"),
    ("LISTA BLANCA", "lista"),
    ("SISTEMA", "sistema"),
    ("REGISTRO", "registro"),
]

_running = True


def _stop(signum, frame):
    """Detiene el bucle principal al recibir SIGTERM o SIGINT.

    @param  signum  senal recibida
    @param  frame   marco de pila actual (no se usa)
    """
    global _running
    _running = False


# Color

_ANSI = re.compile(r"\033\[[0-9;]*m")


def ancho_visible(texto: str) -> int:
    """Calcula la longitud en pantalla de un texto, sin contar las secuencias
    de escape de color.

    @param  texto  texto, posiblemente con secuencias ANSI
    @return numero de caracteres visibles
    """
    return len(_ANSI.sub("", texto))


def recortar(texto: str, ancho: int) -> str:
    """Recorta un texto con colores a un numero de caracteres visibles, para
    que no empuje el menu ni se parta la linea.

    @param  texto  linea, posiblemente con secuencias ANSI
    @param  ancho  caracteres visibles disponibles
    @return la linea, recortada si no cabe
    """
    if ancho_visible(texto) <= ancho:
        return texto
    salida, n, con_color = [], 0, False
    for parte in re.split(f"({_ANSI.pattern})", texto):
        if not parte:
            continue
        if _ANSI.fullmatch(parte):
            salida.append(parte)
            con_color = True
            continue
        cabe = ancho - 1 - n
        if cabe <= 0:
            break
        salida.append(parte[:cabe])
        n += min(len(parte), cabe)
    salida.append("…")
    if con_color:
        salida.append("\033[0m")
    return "".join(salida)


def rellenar(texto: str, ancho: int) -> str:
    """Ajusta una linea a un ancho exacto de caracteres visibles: la recorta
    si sobra o la rellena con espacios si falta.

    @param  texto  linea, posiblemente con secuencias ANSI
    @param  ancho  caracteres visibles deseados
    @return la linea con el ancho indicado
    """
    texto = recortar(texto, ancho)
    falta = ancho - ancho_visible(texto)
    return texto + " " * falta if falta > 0 else texto


class Color:
    """Secuencias ANSI de color. Se anulan si la salida no es un terminal."""

    def __init__(self, activo: bool = True):
        """Activa o desactiva los colores.

        @param  activo  True para emitir secuencias de color
        """
        self.on = activo and sys.stdout.isatty()

    def _c(self, codigo: str, texto: str) -> str:
        """Envuelve un texto en la secuencia ANSI indicada.

        @param  codigo  codigo ANSI
        @param  texto   texto a colorear
        @return texto con color, o el original si los colores estan desactivados
        """
        return f"\033[{codigo}m{texto}\033[0m" if self.on else texto

    def verde(self, t):
        """Colorea un texto en verde.

        @param  t  texto a colorear
        @return texto coloreado
        """
        return self._c("32", t)
    def amarillo(self, t):
        """Colorea un texto en amarillo.

        @param  t  texto a colorear
        @return texto coloreado
        """
        return self._c("33", t)
    def rojo(self, t):
        """Colorea un texto en rojo.

        @param  t  texto a colorear
        @return texto coloreado
        """
        return self._c("31", t)
    def azul(self, t):
        """Colorea un texto en azul.

        @param  t  texto a colorear
        @return texto coloreado
        """
        return self._c("36", t)
    def gris(self, t):
        """Colorea un texto en gris.

        @param  t  texto a colorear
        @return texto coloreado
        """
        return self._c("90", t)
    def fuerte(self, t):
        """Pone un texto en negrita.

        @param  t  texto a colorear
        @return texto en negrita
        """
        return self._c("1", t)
    def inverso(self, t):
        """Muestra un texto en video inverso.

        @param  t  texto a colorear
        @return texto en video inverso
        """
        return self._c("7", t)

    def por_nombre(self, nombre: str, t: str) -> str:
        """Colorea un texto con el color cuyo nombre se indica.

        @param  nombre  nombre del metodo de color (verde, rojo...)
        @param  t       texto a colorear
        @return texto coloreado
        """
        return getattr(self, nombre)(t)

    def limpiar(self) -> str:
        """Devuelve la secuencia que borra la pantalla y lleva el cursor al
        principio.

        @return secuencia de escape, o cadena vacia sin colores
        """
        return "\033[2J\033[H" if self.on else ""

    def inicio(self) -> str:
        """Devuelve la secuencia que lleva el cursor al principio de la pantalla.

        @return secuencia de escape, o cadena vacia sin colores
        """
        return "\033[H" if self.on else ""

    def borrar_resto(self) -> str:
        """Devuelve la secuencia que borra desde el cursor hasta el final de la
        pantalla.

        @return secuencia de escape, o cadena vacia sin colores
        """
        return "\033[J" if self.on else ""

    def cursor(self, visible: bool) -> str:
        """Devuelve la secuencia que muestra u oculta el cursor.

        @param  visible  True para mostrar el cursor
        @return secuencia de escape, o cadena vacia sin colores
        """
        if not self.on:
            return ""
        return "\033[?25h" if visible else "\033[?25l"


# Formato

def barra_snr(snr: float, ancho: int = 18, snr_max: float = 30.0) -> str:
    """Dibuja una barra proporcional al margen de una emision sobre el ruido.

    @param  snr      margen sobre el ruido en dB
    @param  ancho    longitud de la barra en caracteres
    @param  snr_max  margen que llena la barra, en dB
    @return barra de texto
    """
    frac = max(0.0, min(1.0, snr / snr_max))
    llenos = int(frac * ancho)
    return "█" * llenos + "░" * (ancho - llenos)


def margenes_por_columna(picos: dict, suelo: float, f_min: int, f_max: int,
                         ancho: int) -> list:
    """Reduce el espectro a un numero de columnas de margen sobre el ruido.
    Es la base del perfil instantaneo y de la cascada.

    @param  picos  diccionario frecuencia en MHz -> potencia maxima en dB
    @param  suelo  suelo de ruido en dB
    @param  f_min  frecuencia inicial en MHz
    @param  f_max  frecuencia final en MHz
    @param  ancho  numero de columnas
    @return lista de margenes en dB, una por columna
    """
    n_bins = f_max - f_min
    if n_bins <= 0 or ancho <= 0:
        return []

    # Los limites de cada columna se reparten proporcionalmente sobre la
    # banda entera, no con un numero entero de bins por columna. Con un
    # entero, 100 MHz repartidos en 88 columnas daban 1 MHz cada una y
    # solo se pintaban 88: los ultimos 12 MHz desaparecian mientras el
    # eje seguia rotulando 2500 a la derecha. El dibujo quedaba
    # desplazado respecto a su propia escala, que en un instrumento es
    # peor que no dibujar nada.
    columnas = []
    for c in range(ancho):
        lo = f_min + (c * n_bins) // ancho
        hi = f_min + ((c + 1) * n_bins) // ancho
        if hi <= lo:
            hi = lo + 1          # mas columnas que bins: se estira
        mejor = 0.0
        for f in range(lo, hi):
            if f in picos:
                mejor = max(mejor, picos[f] - suelo)
        columnas.append(mejor)

    return columnas


def fila_cascada(col: Color, margenes: list) -> str:
    """Construye una fila de la cascada, con un caracter y un color por
    columna segun el margen.

    @param  col       colores del terminal (objeto Color)
    @param  margenes  margen en dB por columna
    @return linea de texto con color
    """
    trozos = []
    actual = None
    buffer = []

    for m in margenes:
        for corte, caracter, nombre in CASCADA_NIVELES:
            if m >= corte:
                break
        # Se agrupan caracteres consecutivos del mismo color en un solo
        # tramo ANSI. Sin esto, una fila de 90 columnas serian 90
        # secuencias de escape y el repintado parpadearia en un terminal
        # por serie.
        if nombre != actual:
            if buffer:
                trozos.append(col.por_nombre(actual, "".join(buffer)))
            actual, buffer = nombre, []
        buffer.append(caracter)

    if buffer:
        trozos.append(col.por_nombre(actual, "".join(buffer)))
    return "".join(trozos)


def eje_frecuencias(f_min: int, f_max: int, ancho: int) -> str:
    """Dibuja la regla de frecuencias bajo el espectro, con marcas
    intermedias.

    @param  f_min  frecuencia inicial en MHz
    @param  f_max  frecuencia final en MHz
    @param  ancho  numero de columnas
    @return linea de texto con el eje
    """
    if ancho <= 0 or f_max <= f_min:
        return ""

    n_marcas = max(2, min(6, ancho // 14))
    linea = [" "] * ancho

    for i in range(n_marcas):
        pos = int(i * (ancho - 1) / (n_marcas - 1))
        f = f_min + round(i * (f_max - f_min) / (n_marcas - 1))
        etiqueta = str(f)
        # La primera marca se alinea a la izquierda y la ultima a la
        # derecha; las de en medio se centran sobre su posicion.
        if i == 0:
            ini = 0
        elif i == n_marcas - 1:
            ini = ancho - len(etiqueta)
        else:
            ini = pos - len(etiqueta) // 2
        ini = max(0, min(ancho - len(etiqueta), ini))
        for k, c in enumerate(etiqueta):
            linea[ini + k] = c

    return "".join(linea)


def desde(segundos: float) -> str:
    """Expresa un tiempo transcurrido en segundos, minutos u horas.

    @param  segundos  tiempo transcurrido en segundos
    @return texto corto, o "--" si el tiempo es negativo
    """
    if segundos < 0:
        return "--"
    s = int(segundos)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h"


def etiqueta_emision(e: dict) -> str:
    """Genera un texto corto y estable que describe una emision, para el
    registro y la lista de emisiones.

    @param  e  emision
    @return texto con la banda y el tipo
    """
    banda = f"{e['f_inicio_mhz']}–{e['f_fin_mhz']} MHz"
    if e["clase"] == "continua":
        if e["tipo"] == "wifi":
            return f"{banda}  WiFi {e.get('detalle', '')}".strip()
        return f"{banda}  {e['tipo']}"
    return f"{banda}  salto de frecuencia"


def ordenar_emisiones(emisiones: list) -> list:
    """Ordena las emisiones para mostrarlas y seleccionarlas: primero las no
    identificadas y, dentro de cada grupo, las de mayor margen.

    @param  emisiones  lista de emisiones
    @return lista ordenada
    """
    return sorted(emisiones,
                  key=lambda e: (bool(e.get("conocido")), -e["snr_db"]))


def clave_emision(e: dict) -> tuple:
    """Identifica una emision entre ciclos por su banda y su clase, sin la
    potencia ni la persistencia, que fluctuan en cada medida.

    @param  e  emision
    @return tupla (frecuencia inicial, frecuencia final, clase)
    """
    return (e["f_inicio_mhz"], e["f_fin_mhz"], e["clase"])


# Entrada

class Entrada:
    """Lee las pulsaciones del joystick que publica el puente en un fichero
    con un contador creciente.
    """

    def __init__(self):
        """Prepara la lectura sin ningun contador conocido."""
        self._seq = None

    def leer(self):
        """Devuelve la pulsacion nueva, si la hay. En la primera lectura solo
        toma nota del contador, para no ejecutar una pulsacion antigua.

        @return accion pulsada (arriba, abajo, ok, atras) o None
        """
        try:
            with open(ENTRADA_FILE) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

        seq = d.get("seq")
        if seq is None or seq == self._seq:
            return None

        primera = self._seq is None
        self._seq = seq
        # En la primera lectura solo se toma nota del contador. Si el
        # fichero ya existia de antes, ejecutar esa pulsacion al arrancar
        # movaria el menu solo, sin que nadie haya tocado el mando.
        return None if primera else d.get("accion")


class Menu:
    """Estado de navegacion del panel, con el foco en el menu o en el
    contenido de la vista EMISIONES.
    """

    def __init__(self):
        """Inicializa el menu en la vista ESTADO, con el foco en el menu."""
        self.i = 0
        self.pausado = False
        self.foco = "menu"         # "menu" | "contenido"
        self.fila = 0
        self.sub = None            # None | "detalle" | "captura"
        self.seleccion = None
        self.pendiente = None      # accion que ejecuta el bucle principal
        self.captura = None        # estado de la ultima captura fina
        # Hay una alarma de UAV sin reconocer. Mientras la haya, OK en el
        # menu la reconoce en lugar de pausar o entrar: es lo primero que
        # el operador tiene que poder hacer, desde cualquier vista.
        self.alarma_pendiente = False

    def accion(self, a: str, emisiones: list) -> bool:
        """Aplica una pulsacion del joystick al estado de navegacion.

        @param  a          accion pulsada
        @param  emisiones  lista de emisiones, en el orden de ordenar_emisiones
        @return True si algo cambio y hay que repintar
        """
        if self.foco == "menu":
            return self._en_menu(a, emisiones)
        return self._en_contenido(a, emisiones)

    def _en_menu(self, a: str, emisiones: list) -> bool:
        """Aplica una pulsacion con el foco en el menu: cambiar de vista, pausar,
        entrar en las emisiones o reconocer la alarma.

        @param  a          accion pulsada
        @param  emisiones  lista de emisiones, en el orden de ordenar_emisiones
        @return True si algo cambio y hay que repintar
        """
        if a == "ok" and self.alarma_pendiente:
            self.pendiente = "reconocer"
            return True
        if a == "arriba":
            self.i = (self.i - 1) % len(VISTAS)
        elif a == "abajo":
            self.i = (self.i + 1) % len(VISTAS)
        elif a == "ok":
            if self.vista == "emisiones" and emisiones:
                self.foco, self.fila, self.sub = "contenido", 0, None
                self.pausado = True
            else:
                self.pausado = not self.pausado
        elif a == "atras":
            if self.i == 0 and not self.pausado:
                return False
            self.i, self.pausado = 0, False
        else:
            return False
        return True

    def _en_contenido(self, a: str, emisiones: list) -> bool:
        """Aplica una pulsacion con el foco en las emisiones: mover el cursor,
        abrir el detalle, pedir la captura fina o volver.

        @param  a          accion pulsada
        @param  emisiones  lista de emisiones, en el orden de ordenar_emisiones
        @return True si algo cambio y hay que repintar
        """
        if not emisiones:
            self.foco, self.sub, self.pausado = "menu", None, False
            return True

        if self.sub is None:
            if a == "arriba":
                self.fila = max(0, self.fila - 1)
            elif a == "abajo":
                self.fila = min(len(emisiones) - 1, self.fila + 1)
            elif a == "ok":
                self.seleccion = emisiones[self.fila]
                self.sub = "detalle"
            elif a == "atras":
                self.foco, self.sub, self.pausado = "menu", None, False
            else:
                return False
        elif self.sub == "detalle":
            if a == "ok":
                self.pendiente = "captura"
            elif a == "atras":
                self.sub = None
            else:
                return False
        elif self.sub == "captura":
            if a == "atras":
                self.sub = "detalle"
            else:
                return False
        return True

    @property
    def vista(self) -> str:
        """Devuelve el identificador de la vista seleccionada.

        @return nombre interno de la vista
        """
        return VISTAS[self.i][1]


# Salud

class Salud:
    """Estado de la plataforma (temperatura, carga, memoria, servicios),
    leido con poca frecuencia y guardado en cache.
    """

    UNIDADES = ("guard-detector-rf", "guard-bridge")

    def __init__(self):
        """Inicializa la cache vacia."""
        self._cache = {}
        self._ts = 0.0

    def leer(self) -> dict:
        """Lee el estado de la plataforma, o devuelve la cache si es reciente.

        @return diccionario con temperatura, carga, memoria, servicios...
        """
        if time.monotonic() - self._ts < SALUD_TTL_S and self._cache:
            return self._cache

        d = {"temp_c": None, "carga": None, "uptime_s": None,
             "estrangulado": None, "servicios": {}, "memoria": None}

        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                d["temp_c"] = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            pass

        try:
            with open("/proc/loadavg") as f:
                d["carga"] = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass

        try:
            with open("/proc/uptime") as f:
                d["uptime_s"] = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass

        try:
            info = {}
            with open("/proc/meminfo") as f:
                for linea in f:
                    k, _, v = linea.partition(":")
                    info[k] = int(v.split()[0])
            d["memoria"] = (info["MemTotal"] - info["MemAvailable"],
                            info["MemTotal"])
        except (OSError, ValueError, KeyError, IndexError):
            pass

        # Bit 2 de get_throttled = estrangulamiento activo ahora mismo;
        # bit 18 = ha ocurrido desde el arranque. Interesan los dos: el
        # primero explica una medida mala en curso, el segundo delata un
        # equipo que ya se calento aunque ahora este frio.
        salida = self._ejecutar(["vcgencmd", "get_throttled"])
        if salida and "=" in salida:
            try:
                v = int(salida.split("=")[1].strip(), 16)
                d["estrangulado"] = (bool(v & 0x4), bool(v & 0x40000))
            except ValueError:
                pass

        for unidad in self.UNIDADES:
            d["servicios"][unidad] = self._ejecutar(
                ["systemctl", "is-active", unidad]) or "?"

        self._cache = d
        self._ts = time.monotonic()
        return d

    @staticmethod
    def _ejecutar(cmd: list) -> str:
        """Ejecuta una orden del sistema sin que su ausencia o un fallo detengan
        el panel.

        @param  cmd  orden y argumentos
        @return salida de la orden, o cadena vacia si falla
        """
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""


# Fuentes

def leer_snapshot() -> tuple:
    """Lee el analisis que publica el detector.

    @return tupla (analisis, emisiones, antiguedad en s), o (None, None, None) si
        no hay datos legibles
    """
    try:
        antiguedad = time.time() - SNAPSHOT_FILE.stat().st_mtime
        with open(SNAPSHOT_FILE) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None, None

    analisis = {
        "n_barridos": d.get("barridos", 0),
        # El detector declara el estado del receptor en el propio
        # snapshot. Sin ese campo el panel no podria separar «todavia no
        # hay medida» de «el receptor no responde», y son dos cosas muy
        # distintas para quien vigila.
        "receptor": d.get("receptor", "ok"),
        "suelo_ruido_db": d.get("suelo_ruido_db", 0.0),
        "umbral_db": d.get("umbral_db", 0.0),
        "persistencia": {int(k): v
                         for k, v in d.get("persistencia_por_bin", {}).items()},
        "picos": {int(k): v for k, v in d.get("picos_por_bin", {}).items()},
        # Ultimo barrido completo, para el perfil "ahora" y la cascada. Un
        # detector antiguo no lo publica: entonces se usan los picos.
        "seq": d.get("seq"),
        "ultimo": {int(k): v
                   for k, v in d.get("ultimo_barrido", {}).items()},
        "suelo_ultimo_db": d.get("suelo_ultimo_db"),
    }
    return analisis, d.get("emisiones", []), antiguedad


def capturar_propio(args, lista: list) -> tuple:
    """Hace un barrido por cuenta propia cuando el detector no esta en marcha
    (no pueden usar el HackRF a la vez).

    @param  args   argumentos de la linea de ordenes
    @param  lista  lista de emisores conocidos
    @return tupla (analisis, emisiones, antiguedad)
    """
    cap = SimpleNamespace(
        f_min=args.f_min, f_max=args.f_max, ancho_bin=args.ancho_bin,
        barridos=args.barridos, duracion=args.duracion,
    )
    barridos = capturar(cap)
    analisis = analizar(barridos, args.margen)
    emisiones = identificar(analisis)
    marcar_conocidos(emisiones, lista)
    return analisis, emisiones, 0.0


# Menu

def bloque_menu(col: Color, menu: Menu, alto: int) -> list:
    """Dibuja la columna del menu con la vista seleccionada y la ayuda de
    los botones.

    @param  col   colores del terminal (objeto Color)
    @param  menu  estado de navegacion
    @param  alto  alto disponible en lineas
    @return lista de lineas de texto con color
    """
    lineas = [col.gris(" │")]
    for i, (nombre, _) in enumerate(VISTAS):
        if i == menu.i and menu.foco == "menu":
            lineas.append(col.gris(" │ ") +
                          col.fuerte(col.azul(f"▸ {nombre}")))
        elif i == menu.i:
            # El foco esta en el contenido: el menu se apaga para que la
            # atencion vaya a donde actua el joystick.
            lineas.append(col.gris(f" │ ▸ {nombre}"))
        else:
            lineas.append(col.gris(f" │   {nombre}"))

    # La leyenda cambia con el contexto. Una leyenda fija mentiria en la
    # mitad de las pantallas: OK no hace lo mismo en el menu que en el
    # detalle de una emision.
    if menu.foco == "menu":
        ok = ("RECONOCER" if menu.alarma_pendiente
              else "entrar" if menu.vista == "emisiones"
              else "reanudar" if menu.pausado else "pausar")
        ayuda = [("▲▼", "vista"), ("OK", ok), ("◀ ", "inicio")]
    elif menu.sub is None:
        ayuda = [("▲▼", "emisión"), ("OK", "detalle"), ("◀ ", "menú")]
    elif menu.sub == "detalle":
        ayuda = [("OK", "capturar"), ("◀ ", "lista")]
    else:
        ayuda = [("◀ ", "detalle")]

    lineas.append(col.gris(" │"))
    for tecla, texto in ayuda:
        lineas.append(col.gris(f" │ {tecla}  {texto}"))

    while len(lineas) < alto:
        lineas.append(col.gris(" │"))
    return lineas[:alto]


# Clasificador

def leer_clasificacion() -> dict | None:
    """Lee el ultimo resultado del clasificador de UAV.

    @return el resultado con su antiguedad en "_edad" (s), o None si el
        clasificador no ha publicado nada
    """
    try:
        r = json.loads(CLASIF_FILE.read_text())
        r["_edad"] = time.time() - CLASIF_FILE.stat().st_mtime
        return r
    except (OSError, json.JSONDecodeError):
        return None


def texto_clasificacion(col: Color, c: dict | None) -> str:
    """Resume en una linea el estado del clasificador para la cabecera.

    @param  col  colores del terminal (objeto Color)
    @param  c    resultado de leer_clasificacion()
    @return texto con color
    """
    if c is None:
        return col.gris("sin datos del clasificador")
    if c.get("estado") == "sin_modelo":
        return col.amarillo("sin modelo cargado")
    nombre = NOMBRE_CLASE.get(c.get("clase"), c.get("clase", "?"))
    estado = c.get("confirmacion")
    if estado == "pendiente":
        nombre = f"¿{nombre}? confirmando"
    elif estado == "confirmada":
        nombre = f"{nombre} (confirmado)"
    elif estado == "descartada":
        nombre = f"{nombre} (dudoso descartado)"
    texto = (f"{nombre} · {100 * c.get('confianza', 0):.0f} %   "
             f"{c.get('f_centro_mhz', 0):.0f} MHz · hace {desde(c['_edad'])}"
             f" · {c.get('latencia_ms', {}).get('total', 0):.0f} ms")
    if c["_edad"] > CLASIF_VIGENTE_S:
        return col.gris(texto)
    if c.get("alarma"):
        return col.rojo(col.fuerte(texto))
    return col.amarillo(texto) if estado == "pendiente" else col.verde(texto)


def texto_alarma(col: Color, a: dict | None) -> list:
    """Genera las lineas de la alarma de UAV enclavada, mientras no se
    reconozca.

    @param  col  colores del terminal (objeto Color)
    @param  a    estado de la alarma (datos["alarma_uav"]) o None
    @return lista de lineas con color; vacia si no hay alarma pendiente
    """
    if not a:
        return []
    hace = time.monotonic() - a["ultimo_mono"]
    actual = ("se sigue detectando" if a.get("sigue")
              else f"ya no se detecta (última hace {desde(hace)})")
    veces = f", {a['veces']} detecciones" if a["veces"] > 1 else ""
    estilo = lambda t: col.inverso(col.rojo(col.fuerte(t)))
    return [estilo(f" ! ALARMA UAV {a['inicio']} — {a['nombre']} "
                   f"({100 * a['conf']:.0f} %) {a['f']:.0f} MHz{veces} "),
            estilo(f" {actual} · OK para reconocer ")]


def uav_vigente(c: dict | None) -> bool:
    """Indica si hay una alarma de UAV reciente.

    @param  c  resultado de leer_clasificacion()
    @return True si hay una alarma de UAV reciente
    """
    return bool(c and c.get("alarma") and c["_edad"] <= CLASIF_VIGENTE_S)


def uav_posible(c: dict | None) -> bool:
    """Indica si hay una captura dudosa esperando confirmacion.

    @param  c  resultado de leer_clasificacion()
    @return True si hay una captura dudosa esperando confirmacion
    """
    return bool(c and c.get("confirmacion") == "pendiente"
                and c["_edad"] <= CONFIRMA_VIGENTE_S)


# Vistas

def cabecera(col: Color, args, analisis: dict, emisiones: list, lista: list,
             estado_txt: str, ancho: int, fuente: str, antiguedad: float,
             ultima_deteccion: float, pausado: bool,
             clasif_txt: str = "", alarma_txt: list = None) -> list:
    """Dibuja la cabecera del panel con el estado del sistema, el receptor,
    las emisiones y el clasificador.

    @param  col               colores del terminal (objeto Color)
    @param  args              argumentos de la linea de ordenes
    @param  analisis          analisis de la ultima medida
    @param  emisiones         lista de emisiones
    @param  lista             lista de emisores conocidos
    @param  estado_txt        texto del estado del sistema
    @param  ancho             ancho disponible en caracteres
    @param  fuente            origen de los datos (detector o propia)
    @param  antiguedad        antiguedad de los datos en segundos
    @param  ultima_deteccion  instante de la ultima deteccion (monotonic)
    @param  pausado           True si la vista esta congelada
    @param  clasif_txt        resumen del clasificador
    @param  alarma_txt        lineas de la alarma de UAV
    @return lista de lineas de texto con color
    """
    ahora = datetime.now().strftime("%H:%M:%S")
    marca = col.amarillo(" PAUSA ") if pausado else ahora
    titulo = " GUARD — panel de estado "
    # "┌─" + titulo + relleno + " " + marca + " ─┐" ocupa exactamente
    # `ancho`: la linea del menu empieza justo despues.
    relleno = ancho - len(titulo) - ancho_visible(marca) - 6

    def fila(etiqueta: str, izq: str, der: str = "") -> str:
        """Compone una fila de la cabecera en columnas fijas.

        @param  etiqueta  nombre del campo
        @param  izq       valor de la columna izquierda
        @param  der       valor de la columna derecha
        @return linea de texto
        """
        return f"  {etiqueta:<14}{izq:<26}{der}"

    ult = (desde(time.monotonic() - ultima_deteccion)
           if ultima_deteccion > 0 else "--")
    origen = ("detector" if fuente == "detector" else "barrido propio")
    if fuente == "detector" and antiguedad:
        origen += f" · {antiguedad:.0f} s"

    return [
        col.fuerte(f"┌─{titulo}" + "─" * max(0, relleno) + f" {marca} ─┐"),
        "",
        f"  {'ESTADO':<14}{estado_txt}",
        *[f"  {'':<14}{linea}" for linea in (alarma_txt or [])],
        fila("banda", f"{args.f_min}–{args.f_max} MHz",
             f"suelo   {analisis['suelo_ruido_db']} dBFS"),
        fila("origen", origen,
             f"umbral  {analisis['umbral_db']} dBFS"),
        fila("lista blanca", f"{len(lista)} emisor(es)",
             f"última alerta hace {ult}"),
        f"  {'clasificador':<14}{clasif_txt}",
        "",
    ]


def vista_estado(col, args, datos, ancho, alto) -> list:
    """Dibuja la vista ESTADO: espectro, emisiones destacadas y cascada.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  ancho  ancho disponible en caracteres
    @param  alto   alto disponible en lineas
    @return lista de lineas de texto con color
    """
    lineas = bloque_espectro(col, args, datos["margenes"], compacto=True,
                             margenes_max=datos.get("margenes_max"))
    lineas += bloque_emisiones(col, datos, maximo=4, detalle=False,
                               ancho=ancho)

    libre = alto - len(lineas) - 1
    if not args.sin_cascada and libre > 3:
        lineas += bloque_cascada(col, datos, min(8, libre - 2))
    return lineas


def vista_espectro(col, args, datos, ancho, alto) -> list:
    """Dibuja la vista ESPECTRO, con todo el alto para el perfil y la
    cascada.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  ancho  ancho disponible en caracteres
    @param  alto   alto disponible en lineas
    @return lista de lineas de texto con color
    """
    lineas = bloque_espectro(col, args, datos["margenes"], compacto=False,
                             margenes_max=datos.get("margenes_max"))
    libre = alto - len(lineas) - 1
    if libre > 3:
        lineas += bloque_cascada(col, datos, min(CASCADA_MAX, libre - 3))
    lineas.append("")
    lineas.append(col.gris("  ░ 4-8 dB    ▒ 8-14 dB    ▓ 14-20 dB    █ >20 dB"))
    return lineas


def vista_emisiones(col, args, datos, ancho, alto, cursor=None) -> list:
    """Dibuja la vista EMISIONES, con la lista completa y el cursor de
    seleccion.

    @param  col     colores del terminal (objeto Color)
    @param  args    argumentos de la linea de ordenes
    @param  datos   datos de la ultima medida y su historial
    @param  ancho   ancho disponible en caracteres
    @param  alto    alto disponible en lineas
    @param  cursor  fila seleccionada, o None sin cursor
    @return lista de lineas de texto con color
    """
    lineas = bloque_emisiones(col, datos, maximo=max(1, (alto - 5) // 3),
                              detalle=True, ancho=ancho, cursor=cursor)
    if datos["emisiones"] and cursor is None:
        lineas.append(col.gris("  OK para elegir una emisión y ver su detalle"))
    return lineas


def vista_detalle(col, args, datos, menu, ancho, alto) -> list:
    """Dibuja el zoom de una emision con el historial de picos que guarda el
    panel, sin usar el receptor.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  menu   estado de navegacion
    @param  ancho  ancho disponible en caracteres
    @param  alto   alto disponible en lineas
    @return lista de lineas de texto con color
    """
    e = menu.seleccion
    conocida = e.get("conocido")
    marca = (col.verde("CONOCIDA") if conocida
             else col.rojo(col.fuerte("NO IDENTIFICADA")))
    lineas = [f"  {col.fuerte('DETALLE')}   {etiqueta_emision(e)}   {marca}"]

    extra = (f"pico {e['pico_db']} dBFS · SNR +{e['snr_db']:.1f} dB · "
             f"persistencia {e['persistencia'] * 100:.0f} % · "
             f"ancho {e['ancho_mhz']} MHz")
    if e["clase"] != "continua" and "canales_visitados" in e:
        extra += f" · {e['canales_visitados']} canales"
    if e.get("etiqueta"):
        extra += f" · «{e['etiqueta']}»"
    lineas.append(col.gris(f"  {extra[:ancho - 4]}"))
    lineas.append("")

    f_ini, f_fin = e["f_inicio_mhz"], e["f_fin_mhz"]
    pad = max(3, (f_fin - f_ini) // 3)
    zmin = max(args.f_min, f_ini - pad)
    zmax = min(args.f_max, f_fin + 1 + pad)
    w = ancho - 4

    filas = max(3, alto - 12)
    recientes = list(datos["historial_bins"])[-filas:][::-1]
    lineas.append(col.gris(f"  ZOOM {zmin}–{zmax} MHz   (últimos "
                           f"{len(recientes)} barridos, el más nuevo arriba)"))
    if not recientes:
        lineas.append(col.gris("  Sin historial todavía."))
    for picos, suelo in recientes:
        lineas.append("  " + fila_cascada(
            col, margenes_por_columna(picos, suelo, zmin, zmax, w)))

    # Marca bajo el zoom con la banda que ocupa la emision elegida.
    a = int((f_ini - zmin) * w / (zmax - zmin))
    b = int((f_fin + 1 - zmin) * w / (zmax - zmin))
    marca_banda = " " * a + "═" * max(1, b - a)
    lineas.append("  " + (col.verde(marca_banda) if conocida
                          else col.rojo(marca_banda)))
    lineas.append("  " + col.gris(eje_frecuencias(zmin, zmax, w)))
    lineas.append("")
    lineas.append(f"  {col.fuerte('OK')}  captura fina: {CAPTURA_MS:.0f} ms "
                  f"en {e['f_centro_mhz']} MHz ± 10")
    lineas.append(col.gris("      pausa la vigilancia unos 2 s y guarda la "
                           "captura en el dataset"))
    return lineas


def reducir(np, m, filas: int, cols: int):
    """Encoge una matriz tomando el maximo de cada bloque, para no perder
    los picos breves.

    @param  np     modulo numpy
    @param  m      matriz a reducir
    @param  filas  filas de salida
    @param  cols   columnas de salida
    @return matriz reducida
    """
    r = max(1, -(-m.shape[0] // filas))
    c = max(1, -(-m.shape[1] // cols))
    R, C = -(-m.shape[0] // r), -(-m.shape[1] // c)
    m = np.pad(m, ((0, R * r - m.shape[0]), (0, C * c - m.shape[1])),
               constant_values=float(m.min()))
    return m.reshape(R, r, C, c).max(axis=(1, 3))


def vista_captura(col, args, datos, menu, ancho, alto) -> list:
    """Dibuja con caracteres el espectrograma fino de la captura pedida,
    porque la consola HDMI no muestra imagenes.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  menu   estado de navegacion
    @param  ancho  ancho disponible en caracteres
    @param  alto   alto disponible en lineas
    @return lista de lineas de texto con color
    """
    c = menu.captura or {}
    e = menu.seleccion
    lineas = [f"  {col.fuerte('CAPTURA FINA')}   {etiqueta_emision(e)}", ""]

    if c.get("estado") == "esperando":
        seg = time.monotonic() - c["t0"]
        lineas.append(col.amarillo(f"  ● Capturando… {seg:.0f} s   —   "
                                   f"vigilancia en pausa"))
        lineas.append("")
        lineas.append(col.gris("  El detector acaba el barrido en curso, "
                               "pausa el receptor,"))
        lineas.append(col.gris(f"  graba {CAPTURA_MS:.0f} ms en {c['f']} MHz "
                               f"y vuelve a vigilar."))
        return lineas

    if c.get("estado") == "error":
        lineas.append(col.rojo(f"  ✗ {c.get('msg', '')}"[:ancho]))
        if c.get("fichero"):
            lineas.append(col.gris(f"  fichero: {c['fichero']}"[:ancho]))
        return lineas

    import guard_espectrograma as ge
    np = ge.np
    m, info = c["matriz"], c["info"]
    margen_izq = 8
    w = ancho - 2 - margen_izq
    red = reducir(np, m, max(4, alto - 9), w)
    sobre_fondo = red - float(np.median(red))
    ms_fila = info["duracion_ms"] / red.shape[0]

    lineas[-1] = col.gris(f"  {info['fc_hz'] / 1e6:.1f} MHz ± "
                          f"{info['fs_sps'] / 2e6:.0f} · primeros "
                          f"{info['duracion_ms']:.0f} ms · "
                          f"{ms_fila:.2f} ms por fila")
    lineas.append("")
    for i in range(red.shape[0]):
        rot = f"{i * ms_fila:5.1f}ms" if i % 4 == 0 else ""
        lineas.append(f"  {col.gris(f'{rot:<{margen_izq}}')}"
                      + fila_cascada(col, [float(v) for v in sobre_fondo[i]]))
    lineas.append("  " + " " * margen_izq + col.gris(eje_frecuencias(
        int(round(info["f_lo_mhz"])), int(round(info["f_hi_mhz"])),
        red.shape[1])))
    lineas.append("")
    lineas.append(col.gris(f"  guardada en el dataset como sin_etiquetar: "
                           f"{Path(c['fichero']).name}"[:ancho]))
    lineas.append(col.gris("  ░ 4-8   ▒ 8-14   ▓ 14-20   █ >20 dB sobre "
                           "el fondo de la captura"))
    return lineas


def vista_lista(col, args, datos, ancho, alto) -> list:
    """Dibuja la vista LISTA BLANCA con los emisores declarados del entorno.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  ancho  ancho disponible en caracteres
    @param  alto   alto disponible en lineas
    @return lista de lineas de texto con color
    """
    lista = datos["lista"]
    lineas = [col.gris("  LISTA BLANCA   (emisores declarados del entorno)"), ""]
    if not lista:
        lineas.append(col.amarillo("  Vacía: toda emisión se reporta como no "
                                   "identificada."))
        lineas.append("")
        lineas.append(col.gris("  Se genera con:  guard_rf.py --calibrar"))
        return lineas

    lineas.append(f"  {'banda':<18}{'clase':<20}{'etiqueta'}")
    lineas.append(col.gris("  " + "─" * (ancho - 4)))
    for e in lista[:max(1, alto - 8)]:
        banda = f"{e.get('f_inicio_mhz', '?')}–{e.get('f_fin_mhz', '?')} MHz"
        lineas.append(f"  {banda:<18}{e.get('clase', 'cualquiera'):<20}"
                      f"{e.get('etiqueta', 'sin etiqueta')}")

    lineas.append("")
    lineas.append(col.gris("  Una entrada solo silencia emisiones de su misma "
                           "clase: un WiFi declarado"))
    lineas.append(col.gris("  no enmascara una FHSS que comparta banda."))
    return lineas


def vista_sistema(col, args, datos, ancho, alto) -> list:
    """Dibuja la vista SISTEMA con la salud de la plataforma y el estado de
    los servicios.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  ancho  ancho disponible en caracteres
    @param  alto   alto disponible en lineas
    @return lista de lineas de texto con color
    """
    s = datos["salud"]
    lineas = [col.gris("  PLATAFORMA"), ""]

    def fila(k, v):
        """Compone una fila de la vista SISTEMA.

        @param  k  nombre del campo
        @param  v  valor
        @return linea de texto
        """
        return f"  {k:<22}{v}"

    if s["temp_c"] is not None:
        t = s["temp_c"]
        # 80 grados es donde el benchmark empezo a ver degradacion; 70 es
        # el aviso para tenerlo a la vista antes de que pase.
        txt = (col.rojo(f"{t:.1f} °C") if t >= 80
               else col.amarillo(f"{t:.1f} °C") if t >= 70
               else col.verde(f"{t:.1f} °C"))
        lineas.append(fila("temperatura", txt))

    if s["estrangulado"] is not None:
        ahora, alguna_vez = s["estrangulado"]
        lineas.append(fila("estrangulamiento",
                           col.rojo("activo ahora mismo") if ahora
                           else col.amarillo("ocurrió desde el arranque")
                           if alguna_vez else col.verde("nunca")))

    if s["carga"] is not None:
        lineas.append(fila("carga media (1 min)", f"{s['carga']:.2f}"))

    if s["memoria"]:
        usada, total = s["memoria"]
        lineas.append(fila("memoria",
                           f"{usada // 1024} / {total // 1024} MB"))

    if s["uptime_s"] is not None:
        lineas.append(fila("activo desde hace", desde(s["uptime_s"])))

    lineas.append("")
    lineas.append(col.gris("  SERVICIOS"))
    lineas.append("")
    for unidad, estado in s["servicios"].items():
        lineas.append(fila(unidad,
                           col.verde(estado) if estado == "active"
                           else col.rojo(estado)))

    lineas.append("")
    lineas.append(col.gris("  CONFIGURACIÓN"))
    lineas.append("")
    lineas.append(fila("banda", f"{args.f_min}–{args.f_max} MHz"))
    lineas.append(fila("ancho de bin", f"{args.ancho_bin // 1000} kHz"))
    lineas.append(fila("margen de detección", f"{args.margen} dB"))
    lineas.append(fila("barridos por análisis",
                       str(datos["analisis"]["n_barridos"])))
    return lineas


def vista_registro(col, args, datos, ancho, alto) -> list:
    """Dibuja la vista REGISTRO con las emisiones nuevas, la mas reciente
    arriba.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  ancho  ancho disponible en caracteres
    @param  alto   alto disponible en lineas
    @return lista de lineas de texto con color
    """
    registro = datos["registro"]
    lineas = [col.gris("  REGISTRO   (emisiones nuevas, la más reciente "
                       "arriba)"), ""]
    if not registro:
        lineas.append(col.gris("  Todavía no se ha registrado ninguna."))
        return lineas
    for hora, texto, conocida in list(registro)[-(alto - 4):][::-1]:
        marca = col.gris("·") if conocida else col.rojo("!")
        etiqueta = col.gris(texto) if conocida else texto
        lineas.append(f"  {col.gris(hora)}  {marca}  {etiqueta}")
    return lineas


VISTAS_FN = {
    "estado": vista_estado,
    "espectro": vista_espectro,
    "emisiones": vista_emisiones,
    "lista": vista_lista,
    "sistema": vista_sistema,
    "registro": vista_registro,
}


# Bloques

def bloque_espectro(col: Color, args, margenes: list,
                    compacto: bool, margenes_max: list | None = None) -> list:
    """Dibuja el perfil del espectro con su eje: el ultimo barrido y el
    maximo de la ventana.

    @param  col           colores del terminal (objeto Color)
    @param  args          argumentos de la linea de ordenes
    @param  margenes      margen sobre el ruido por columna, ultimo barrido
    @param  compacto      vista de resumen (una sola linea)
    @param  margenes_max  margen por columna, maximo de la ventana
    @return lista de lineas de texto con color
    """
    titulo = ("  ESPECTRO" if compacto
              else "  ESPECTRO   (color = margen sobre el ruido, "
                   "escala de la cascada)")
    lineas = [col.gris(titulo)]
    if margenes_max is None:
        lineas.append("  " + fila_cascada(col, margenes))
    elif compacto:
        lineas.append("  " + fila_cascada(col, margenes_max)
                      + col.gris("  máx 15 s"))
    else:
        lineas.append("  " + fila_cascada(col, margenes)
                      + col.gris("  ahora"))
        lineas.append("  " + fila_cascada(col, margenes_max)
                      + col.gris("  máx 15 s"))
    lineas += ["  " + col.gris(eje_frecuencias(args.f_min, args.f_max,
                                               len(margenes))), ""]
    return lineas


def bloque_cascada(col: Color, datos: dict, filas: int) -> list:
    """Dibuja la cascada con las ultimas medidas, la mas nueva arriba.

    @param  col    colores del terminal (objeto Color)
    @param  datos  datos de la ultima medida y su historial
    @param  filas  numero de filas disponibles
    @return lista de lineas; vacia si no hay historial o espacio
    """
    historial, columnas = datos["historial"], len(datos["margenes"])
    if filas <= 0 or not historial:
        return []

    # La mas reciente arriba: la vista natural de una cascada es que lo
    # nuevo entra por donde esta la mirada, junto al perfil instantaneo.
    recientes = list(historial)[-filas:][::-1]
    unidad = ("barridos de 1,5 s" if datos.get("margenes_max") is not None
              else "ciclos")
    lineas = [col.gris(f"  RECIENTE   (últimos {len(recientes)} {unidad}, "
                       f"el más nuevo arriba)")]
    for margenes in recientes:
        # Si el terminal ha cambiado de ancho, las filas guardadas antes
        # tienen otra longitud. Se ajustan al ancho actual para que la
        # cascada no quede dentada tras un redimensionado.
        if len(margenes) != columnas:
            margenes = (margenes[:columnas] if len(margenes) > columnas
                        else margenes + [0.0] * (columnas - len(margenes)))
        lineas.append("  " + fila_cascada(col, margenes))
    return lineas


def bloque_emisiones(col: Color, datos: dict, maximo: int,
                     detalle: bool, ancho: int, cursor=None) -> list:
    """Dibuja la tabla de emisiones, primero las no identificadas.

    @param  col      colores del terminal (objeto Color)
    @param  datos    datos de la ultima medida y su historial
    @param  maximo   numero maximo de filas
    @param  detalle  True para mostrar todas las columnas
    @param  ancho    ancho disponible en caracteres
    @param  cursor   fila seleccionada, o None sin cursor
    @return lista de lineas de texto con color
    """
    emisiones = datos["emisiones"]
    desconocidas = [e for e in emisiones if not e.get("conocido")]

    if not emisiones:
        return [col.gris("  Sin emisiones por encima del umbral."), ""]

    # Las no identificadas primero, y dentro de cada grupo las de mayor
    # margen: si no caben todas, las que se pierden son las que menos
    # importan.
    orden = ordenar_emisiones(emisiones)
    # Con cursor, la ventana se desplaza para que la fila elegida se vea
    # siempre, aunque este mas abajo de lo que cabe.
    inicio = 0 if cursor is None else max(0, cursor - max(1, maximo) + 1)
    mostradas = orden[inicio:inicio + max(0, maximo)]

    # El ancho de la etiqueta se calcula, no se fija: si desbordara la
    # columna de contenido, empujaria el menu de la derecha y la pantalla
    # quedaria dentada linea a linea.
    w_barra = 14
    w_et = max(20, ancho - w_barra - 41)

    lineas = [col.gris(f"  EMISIONES   ({len(emisiones)}, "
                       f"{len(desconocidas)} sin identificar)"), ""]

    for n, e in enumerate(mostradas, start=inicio):
        elegida = cursor is not None and n == cursor
        pre = col.fuerte(col.azul("▸ ")) if elegida else "  "
        if e.get("conocido"):
            marca = col.verde("CONOCIDA")
            barra = col.gris(barra_snr(e["snr_db"], ancho=w_barra))
        else:
            marca = col.rojo(col.fuerte("NO IDENTIFICADA"))
            barra = col.rojo(barra_snr(e["snr_db"], ancho=w_barra))

        lineas.append(f"{pre}{etiqueta_emision(e)[:w_et]:<{w_et}}{barra}  "
                      f"+{e['snr_db']:.1f} dB  "
                      f"pers. {e['persistencia'] * 100:.0f} %  {marca}")

        if detalle:
            extra = (f"pico {e['pico_db']} dBFS · ancho {e['ancho_mhz']} MHz "
                     f"· centro {e['f_centro_mhz']} MHz")
            if e["clase"] != "continua" and "canales_visitados" in e:
                extra += f" · {e['canales_visitados']} canales visitados"
            if e.get("etiqueta"):
                extra += f" · «{e['etiqueta']}»"
            lineas.append(col.gris(f"    {extra[:ancho - 6]}"))
            lineas.append("")

    ocultas = len(orden) - len(mostradas)
    if ocultas > 0:
        lineas.append(col.gris(f"  … y {ocultas} más (no caben en pantalla)"))
    if not detalle:
        lineas.append("")
    return lineas


# Pintado

def pintar(col: Color, args, datos: dict, menu: Menu, ciclo: int) -> None:
    """Compone y escribe la pantalla completa: cabecera, menu y vista activa.

    @param  col    colores del terminal (objeto Color)
    @param  args   argumentos de la linea de ordenes
    @param  datos  datos de la ultima medida y su historial
    @param  menu   estado de navegacion
    @param  ciclo  numero de ciclo de medida
    """
    term = shutil.get_terminal_size((100, 30))
    ancho = max(70, min(term.columns - 2, 130))
    alto = max(16, term.lines - 1)
    ancho_c = ancho - ANCHO_MENU

    analisis = datos["analisis"]
    emisiones = datos["emisiones"]
    desconocidas = [e for e in emisiones if not e.get("conocido")]
    obsoleto = (datos["fuente"] == "detector"
                and datos["antiguedad"] > ANTIGUEDAD_MAX_S)

    # --- estado, replicando la maquina de estados que tenia el ESP32
    # Un snapshot obsoleto se trata como perdida de enlace: preferible
    # declarar que no hay informacion fiable a mostrar datos viejos como
    # si fueran actuales.
    if obsoleto:
        estado_txt = col.rojo(col.fuerte(
            f"● DETECTOR CAIDO — sin datos desde hace "
            f"{desde(datos['antiguedad'])}"))
    elif analisis.get("receptor") == "sin_datos":
        # El detector esta vivo y publica: lo que falla es el receptor.
        estado_txt = col.rojo(col.fuerte("● RECEPTOR SIN SEÑAL — no se vigila"))
    elif analisis["n_barridos"] == 0:
        estado_txt = col.amarillo("● ESPERANDO AL DETECTOR")
    elif uav_vigente(datos.get("clasif")):
        # Por encima de "actividad no identificada": es mas especifico y
        # es lo que el sistema existe para detectar.
        c = datos["clasif"]
        estado_txt = col.rojo(col.fuerte(
            f"● UAV PROBABLE — {NOMBRE_CLASE.get(c['clase'], c['clase'])} "
            f"({100 * c['confianza']:.0f} %) cerca de "
            f"{c.get('f_centro_mhz', 0):.0f} MHz"))
    elif uav_posible(datos.get("clasif")):
        # Una captura dudosa no da alarma: se avisa en amarillo mientras
        # llega la captura de confirmacion (unos segundos).
        c = datos["clasif"]
        estado_txt = col.amarillo(col.fuerte(
            f"● POSIBLE UAV — confirmando "
            f"({NOMBRE_CLASE.get(c['clase'], c['clase'])}, "
            f"{100 * c['confianza']:.0f} %) cerca de "
            f"{c.get('f_centro_mhz', 0):.0f} MHz"))
    elif desconocidas:
        estado_txt = col.rojo(col.fuerte("● ALERTA — actividad no identificada"))
    else:
        estado_txt = col.verde("● OPERATIVO — sin actividad no identificada")

    # La cabecera y el estado salen en TODAS las vistas. Una alerta no
    # puede depender de en que pantalla este el operador.
    contenido = cabecera(col, args, analisis, emisiones, datos["lista"],
                         estado_txt, ancho_c, datos["fuente"],
                         datos["antiguedad"], datos["ultima_deteccion"],
                         menu.pausado,
                         texto_clasificacion(col, datos.get("clasif")),
                         texto_alarma(col, datos.get("alarma_uav")))

    alto_vista = alto - len(contenido) - 2
    if menu.vista == "emisiones" and menu.sub == "detalle":
        contenido += vista_detalle(col, args, datos, menu, ancho_c, alto_vista)
    elif menu.vista == "emisiones" and menu.sub == "captura":
        contenido += vista_captura(col, args, datos, menu, ancho_c, alto_vista)
    elif menu.vista == "emisiones":
        cursor = menu.fila if menu.foco == "contenido" else None
        contenido += vista_emisiones(col, args, datos, ancho_c, alto_vista,
                                     cursor=cursor)
    else:
        contenido += VISTAS_FN[menu.vista](col, args, datos, ancho_c,
                                           alto_vista)

    menu_lineas = bloque_menu(col, menu, len(contenido))

    salida = []
    for i in range(max(len(contenido), len(menu_lineas))):
        izq = contenido[i] if i < len(contenido) else ""
        der = menu_lineas[i] if i < len(menu_lineas) else ""
        salida.append(rellenar(izq, ancho_c) + der)

    salida.append("")
    salida.append(col.gris(f"  ciclo {ciclo}   ·   Ctrl+C para salir"))

    sys.stdout.write(col.inicio() + "\n".join(salida) + "\n" +
                     col.borrar_resto())
    sys.stdout.flush()


# Captura fina

def pedir_captura(menu: Menu) -> None:
    """Deja de forma atomica la peticion de captura fina para el detector.

    @param  menu  estado de navegacion, con la emision seleccionada
    """
    if menu.captura and menu.captura["estado"] == "esperando":
        return
    e = menu.seleccion
    pid = int(time.time() * 1000)
    peticion = {
        "id": pid,
        "accion": "captura_fina",
        "f_centro_mhz": e["f_centro_mhz"],
        "ms": CAPTURA_MS,
        "etiqueta": "sin_etiquetar",
        # La nota dice que se vio y como se clasifico en ese momento. Es
        # lo que permitira etiquetar la captura despues con criterio.
        "nota": (f"desde el panel: {etiqueta_emision(e)}, "
                 f"{'conocida' if e.get('conocido') else 'no identificada'}, "
                 f"SNR {e['snr_db']} dB, persistencia {e['persistencia']}"),
    }
    menu.captura = {"estado": "esperando", "id": pid,
                    "t0": time.monotonic(), "f": e["f_centro_mhz"]}
    try:
        tmp = PETICION_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(peticion, f, separators=(",", ":"))
        os.replace(tmp, PETICION_FILE)
    except OSError as exc:
        menu.captura.update(estado="error",
                            msg=f"no se pudo dejar la petición: {exc}")


def revisar_captura(menu: Menu) -> bool:
    """Comprueba si el detector ya respondio a la peticion de captura fina.

    @param  menu  estado de navegacion
    @return True si el estado de la captura cambio
    """
    c = menu.captura
    try:
        r = json.loads(RESPUESTA_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        r = None

    # Una respuesta con otro id es de una peticion anterior: se ignora.
    if r and r.get("id") == c["id"]:
        if r.get("estado") == "ok":
            try:
                from guard_espectrograma import calcular_iq
                m, info = calcular_iq(Path(r["fichero"]), nfft=1024,
                                      filas_max=400, ms=CAPTURA_VER_MS)
                c.update(estado="ok", matriz=m, info=info,
                         fichero=r["fichero"])
            except (ImportError, RuntimeError, OSError, ValueError) as exc:
                c.update(estado="error", fichero=r.get("fichero"),
                         msg=f"capturada, pero no se pudo dibujar: {exc}")
        else:
            c.update(estado="error", msg=r.get("msg", "error desconocido"))
        return True

    if time.monotonic() - c["t0"] > CAPTURA_TIMEOUT_S:
        c.update(estado="error", msg="el detector no respondió. "
                                     "¿Está en marcha guard-detector-rf?")
        return True
    return False


# Main

def main() -> int:
    """Punto de entrada: lee los argumentos y ejecuta el bucle que atiende
    el joystick y refresca las medidas.

    @return codigo de salida
    """
    p = argparse.ArgumentParser(description="Panel de estado en terminal")
    p.add_argument("--f-min", type=int, default=2400)
    p.add_argument("--f-max", type=int, default=2500)
    p.add_argument("--ancho-bin", type=int, default=1_000_000)
    p.add_argument("--barridos", type=int, default=8,
                   help="barridos por ciclo en modo autonomo (def. 8)")
    p.add_argument("--duracion", type=float, default=1.0,
                   help="segundos por barrido individual")
    p.add_argument("--margen", type=float, default=8.0)
    p.add_argument("--config", type=Path, default=CONFIG_DEF)
    p.add_argument("--sin-color", action="store_true")
    p.add_argument("--sin-cascada", action="store_true",
                   help="oculta la cascada (pantallas de muy poca altura)")
    p.add_argument("--autonomo", action="store_true",
                   help="barrer por cuenta propia en lugar de consumir el "
                        "analisis del detector (no usar con guard_rf activo)")
    p.add_argument("--sin-receptor", action="store_true",
                   help="no abrir nunca el HackRF: espera al detector. "
                        "Obligatorio al correr como servicio junto a guard_rf")
    args = p.parse_args()

    if args.autonomo and args.sin_receptor:
        print("ERROR: --autonomo y --sin-receptor se excluyen.", file=sys.stderr)
        return 1

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    col = Color(not args.sin_color)
    salud = Salud()
    entrada = Entrada()
    menu = Menu()

    datos = {
        "analisis": {"n_barridos": 0, "receptor": "ok", "suelo_ruido_db": 0.0,
                     "umbral_db": 0.0, "persistencia": {}, "picos": {}},
        "emisiones": [],
        "lista": cargar_lista_blanca(args.config),
        "historial": deque(maxlen=CASCADA_MAX),
        # Picos por bin de cada ciclo, sin reducir a columnas: el zoom del
        # detalle necesita la resolucion original para ampliar una banda.
        "historial_bins": deque(maxlen=CASCADA_MAX),
        "registro": deque(maxlen=REGISTRO_MAX),
        "salud": salud.leer(),
        "margenes": [],
        "fuente": "detector",
        "antiguedad": 0.0,
        "ultima_deteccion": 0.0,
        "alarma_uav": None,
    }

    presentes = set()
    ts_previo = None
    ciclo = 0
    proximo_dato = 0.0
    ultimo_pintado = 0.0

    sys.stdout.write(col.limpiar() + col.cursor(False))
    sys.stdout.flush()

    try:
        while _running:
            repintar = False

            # --- entrada: se mira a 10 Hz, independientemente del analisis
            accion = entrada.leer()
            if accion and menu.accion(accion,
                                      ordenar_emisiones(datos["emisiones"])):
                repintar = True

            # --- alarma de UAV: OK la reconoce desde cualquier vista
            menu.alarma_pendiente = datos.get("alarma_uav") is not None
            if menu.pendiente == "reconocer":
                menu.pendiente = None
                a = datos["alarma_uav"]
                if a:
                    veces = (f", {a['veces']} detecciones"
                             if a["veces"] > 1 else "")
                    datos["registro"].append((
                        datetime.now().strftime("%H:%M:%S"),
                        f"alarma reconocida por el operador: {a['nombre']} "
                        f"desde las {a['inicio']}{veces}", True))
                datos["alarma_uav"] = None
                menu.alarma_pendiente = False
                repintar = True

            # --- captura fina: pedirla y esperar la respuesta del detector
            if menu.pendiente == "captura":
                menu.pendiente = None
                pedir_captura(menu)
                menu.sub = "captura"
                repintar = True
            if menu.captura and menu.captura["estado"] == "esperando":
                if revisar_captura(menu):
                    repintar = True
                elif time.monotonic() - ultimo_pintado >= 1.0:
                    repintar = True      # el contador de segundos avanza

            # --- datos: a su propio ritmo, y detenidos si el operador paso
            if not menu.pausado and time.monotonic() >= proximo_dato:
                ciclo += 1
                actualizar_datos(args, datos, salud, presentes, ts_previo)
                ts_previo = datos["_ts"]
                presentes = datos["_presentes"]
                proximo_dato = time.monotonic() + (
                    PERIODO_DATOS_S if datos["fuente"] == "detector" else 0.0)
                repintar = True

            if repintar and _running:
                pintar(col, args, datos, menu, ciclo)
                ultimo_pintado = time.monotonic()

            if _running:
                time.sleep(PERIODO_ENTRADA_S)
    finally:
        sys.stdout.write(col.cursor(True))
        sys.stdout.write("\n  panel detenido\n")
        sys.stdout.flush()

    return 0


def actualizar_datos(args, datos: dict, salud: Salud, presentes: set,
                     ts_previo) -> None:
    """Toma una medida nueva y actualiza el historial, el registro y la
    salud de la plataforma.

    @param  args       argumentos de la linea de ordenes
    @param  datos      datos de la ultima medida y su historial
    @param  salud      lector de la salud de la plataforma
    @param  presentes  claves de las emisiones del ciclo anterior
    @param  ts_previo  marca de tiempo del analisis anterior
    """
    # El detector, si esta corriendo, es el unico que habla con el
    # receptor: el panel consume su analisis en lugar de competir por el
    # HackRF. Solo barre por su cuenta si no hay detector.
    analisis = emisiones = None
    fuente, antiguedad = "propia", 0.0

    if not args.autonomo:
        analisis, emisiones, antiguedad = leer_snapshot()
        if analisis is not None:
            fuente = "detector"

    if analisis is None:
        if args.sin_receptor:
            # Ejecutandose como servicio junto al detector: no debe abrir
            # el HackRF bajo ningun concepto, porque se lo quitaria.
            analisis = {"n_barridos": 0, "receptor": "ok",
                        "suelo_ruido_db": 0.0, "umbral_db": 0.0,
                        "persistencia": {}, "picos": {}}
            emisiones, fuente, antiguedad = [], "detector", 0.0
        else:
            analisis, emisiones, antiguedad = capturar_propio(
                args, datos["lista"])
            fuente = "propia"

    datos.update({"analisis": analisis, "emisiones": emisiones,
                  "fuente": fuente, "antiguedad": antiguedad,
                  "salud": salud.leer()})

    # Clasificador de UAV: cada resultado nuevo con alarma se anota en el
    # registro, una sola vez (se identifica por su marca de tiempo).
    clasif = leer_clasificacion()
    datos["clasif"] = clasif
    nueva = bool(clasif and clasif.get("ts")
                 and clasif.get("ts") != datos.get("_clasif_ts"))
    if nueva and not clasif.get("alarma"):
        datos["_clasif_ts"] = clasif.get("ts")
        # Una clasificacion nueva sin alarma dice que el UAV ya no se ve,
        # pero la alarma enclavada sigue hasta que se reconozca.
        if datos.get("alarma_uav"):
            datos["alarma_uav"]["sigue"] = False
        nombre = NOMBRE_CLASE.get(clasif.get("clase"), clasif.get("clase"))
        hora = datetime.now().strftime("%H:%M:%S")
        f = clasif.get("f_centro_mhz") or 0
        if clasif.get("confirmacion") == "pendiente":
            datos["registro"].append((
                hora, f"posible UAV: {nombre} "
                f"({100 * clasif['confianza']:.0f} %) cerca de {f:.0f} MHz"
                " — se repite la captura para confirmar", False))
        elif clasif.get("confirmacion") == "descartada":
            d = clasif.get("descarta_a") or {}
            dud = NOMBRE_CLASE.get(d.get("clase"), d.get("clase", "?"))
            datos["registro"].append((
                hora, f"posible UAV descartado ({dud}, "
                f"{100 * d.get('confianza', 0):.0f} %): la captura de "
                f"confirmación en {f:.0f} MHz no lo ve", False))
    if nueva and clasif.get("alarma"):
        datos["_clasif_ts"] = clasif.get("ts")
        nombre = NOMBRE_CLASE.get(clasif["clase"], clasif["clase"])
        hora = datetime.now().strftime("%H:%M:%S")
        como = (" — confirmado en 2 capturas"
                if clasif.get("confirmacion") == "confirmada" else "")
        datos["registro"].append((
            hora,
            f"UAV probable: {nombre} ({100 * clasif['confianza']:.0f} %) "
            f"cerca de {clasif.get('f_centro_mhz', 0):.0f} MHz{como}", False))
        datos["ultima_deteccion"] = time.monotonic()
        # Alarma enclavada: se queda en pantalla hasta que el operador la
        # reconoce, aunque las clasificaciones siguientes digan "sin UAV".
        # Un dron que pasa en 15 s no puede depender de que alguien
        # estuviera mirando justo en ese momento. Las detecciones que
        # llegan mientras no se ha reconocido son del mismo episodio.
        a = datos.get("alarma_uav")
        if a is None:
            datos["alarma_uav"] = {
                "inicio": hora, "nombre": nombre,
                "conf": clasif["confianza"],
                "f": clasif.get("f_centro_mhz", 0),
                "ultimo_mono": time.monotonic(), "veces": 1, "sigue": True}
        else:
            a["veces"] += 1
            a["ultimo_mono"] = time.monotonic()
            a["sigue"] = True
            if clasif["confianza"] >= a["conf"]:
                a.update(nombre=nombre, conf=clasif["confianza"],
                         f=clasif.get("f_centro_mhz", 0))

    term = shutil.get_terminal_size((100, 30))
    ancho_c = max(70, min(term.columns - 2, 130)) - ANCHO_MENU
    margenes_max = margenes_por_columna(
        analisis["picos"], analisis["suelo_ruido_db"],
        args.f_min, args.f_max, ancho_c - ANCHO_ETIQUETA)
    ultimo = analisis.get("ultimo")
    if ultimo:
        # Detector con ventana deslizante: el perfil "ahora" y la cascada
        # salen del ultimo barrido, cada uno con su propio suelo.
        suelo_u = analisis.get("suelo_ultimo_db") or analisis["suelo_ruido_db"]
        datos["margenes"] = margenes_por_columna(
            ultimo, suelo_u, args.f_min, args.f_max, ancho_c - ANCHO_ETIQUETA)
        datos["margenes_max"] = margenes_max
        bins_fila = (dict(ultimo), suelo_u)
    else:
        datos["margenes"] = margenes_max
        datos["margenes_max"] = None
        bins_fila = (dict(analisis["picos"]), analisis["suelo_ruido_db"])

    # Solo cuenta como medida nueva si el analisis ha cambiado; de lo
    # contrario un snapshot estancado llenaria la cascada de copias de la
    # misma foto y el registro de falsas novedades. Con el detector actual
    # se usa el numero de barrido, que identifica cada uno sin ambiguedad.
    ts_actual = (analisis.get("seq") or
                 (analisis["n_barridos"], analisis["suelo_ruido_db"],
                  len(emisiones)))
    datos["_ts"] = ts_actual
    datos["_presentes"] = presentes

    if ts_actual == ts_previo:
        return

    datos["historial"].append(list(datos["margenes"]))
    datos["historial_bins"].append(bins_fila)

    claves = {clave_emision(e): e for e in emisiones}
    hora = datetime.now().strftime("%H:%M:%S")
    for clave, e in claves.items():
        if clave not in presentes:
            datos["registro"].append((hora, etiqueta_emision(e),
                                      bool(e.get("conocido"))))
    datos["_presentes"] = set(claves)

    if any(not e.get("conocido") for e in emisiones):
        datos["ultima_deteccion"] = time.monotonic()


if __name__ == "__main__":
    sys.exit(main())
