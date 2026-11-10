# GUARD — Sistema para la detección de emisiones de radiofrecuencia mediante SDR

Software, configuración y documentación técnica del dispositivo GUARD,
desarrollado como Trabajo Fin de Grado (Grado en Ingeniería Informática,
Universidad de Granada).

GUARD es un equipo embebido de bajo coste que vigila la banda de 2,4 GHz,
detecta las emisiones no identificadas y clasifica las de salto de
frecuencia para avisar de la presencia de un UAV. Se compone de:

- Raspberry Pi 4 Model B (2 GB) con Raspberry Pi OS Lite de 64 bits
- Receptor SDR HackRF One con antena telescópica ANT500
- ESP32-WROVER (Freenove) como periférico de mando y aviso: joystick,
  LEDs y zumbador, conectado por UART
- Reloj de tiempo real DS3231 por I2C
- Monitor HDMI para el panel de operador

## Estructura

| Ruta | Contenido |
|---|---|
| `system/os-setup/` | Scripts de aprovisionamiento idempotentes `01`–`05` |
| `system/systemd/` | Unidades de los servicios y sus ficheros complementarios (`*.service.d/`) |
| `system/bin/guard-silent-mode` | Modo RF-silente (`on` / `off` / `status`) |
| `system/udev/` | Regla udev del HackRF |
| `system/boot/config.txt` | `config.txt` de referencia del dispositivo |
| `tools/` | Detector de actividad RF, puente con el ESP32, panel, captura IQ, espectrogramas y clasificador |
| `experimentos/` | Scripts de evaluación de las reglas de alarma y de la confirmación |
| `modelo/` | Modelo del clasificador desplegado en el equipo (`.npz`) |
| `detector-stub/` | Detector simulado para probar la cadena de aviso sin receptor |
| `esp32-firmware/` | Firmware del ESP32 |
| `benchmark/` | Benchmarks de inferencia y de la cadena completa en la Pi |
| `config/lista_blanca.json` | Lista blanca de emisores conocidos |
| `tests/` | Pruebas unitarias, de integración y de sistema, con sus informes |
| `datos/` | Rasgos (`rasgos*.csv`) y manifiestos de las capturas usadas para entrenar y evaluar |
| `resultados/` | Resultados de la evaluación del clasificador |
| `reports/` | Medidas de arranque y resultados de los benchmarks |
| `docs/` | Plan de integración y protocolo UART |
| `hardware/fotos/` | Fotografías del equipo y del panel |

Las grabaciones IQ (`.cs8`) no se incluyen por su tamaño.

## Reconstrucción del dispositivo

Partiendo de una tarjeta con Raspberry Pi OS Lite de 64 bits recién
instalada y un usuario `guard` con permisos de administrador:

```bash
# 1. Paquetes
sudo apt update
sudo apt install -y git hackrf python3-numpy python3-serial python3-matplotlib i2c-tools

# 2. Repositorio
sudo git clone <URL de este repositorio> /opt/guard_device
sudo chown -R guard:guard /opt/guard_device
cd /opt/guard_device

# 3. Aprovisionamiento del sistema operativo
sudo bash system/os-setup/01-base.sh
sudo bash system/os-setup/02-watchdog.sh
sudo bash system/os-setup/03-boot-trim.sh
sudo bash system/os-setup/04-uart-esp32.sh
sudo bash system/os-setup/05-rtc.sh

# 4. Modo silente, regla udev y servicios
sudo install -m 755 system/bin/guard-silent-mode /usr/local/sbin/
sudo cp system/udev/60-libhackrf0.rules /etc/udev/rules.d/
sudo cp -r system/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable guard-silent-mode guard-detector-rf guard-clasificador guard-bridge guard-panel

# 5. Reinicio y comprobación
sudo reboot
sudo bash system/os-setup/05-rtc.sh --verificar
sudo python3 tests/ejecutar_pruebas.py
```

El firmware del ESP32 se carga desde el IDE de Arduino
(ver `esp32-firmware/README.md`). Para probar la cadena de aviso sin
receptor, se sustituye `guard-detector-rf` por `guard-detector-stub`.

## Clasificador

El modelo se entrena en un ordenador con Python, NumPy y scikit-learn a
partir de los rasgos de `datos/`, y se exporta a `.npz` para ejecutarlo en
la Pi solo con NumPy:

```bash
# Evaluación (detección con el conjunto externo y falsas alarmas con las sesiones propias 2 y 3)
python3 tools/guard_evaluar.py datos/dataset/rasgos.csv datos/dataset/manifest.jsonl \
    datos/dataset_s3/rasgos_s2s3.csv datos/dataset_s3/manifest_s2s3.jsonl

# Exportación del modelo para la Pi
python3 tools/guard_modelo.py --datos datos/dataset/rasgos.csv datos/dataset/manifest.jsonl \
    --datos datos/dataset_s3/rasgos_s2s3.csv datos/dataset_s3/manifest_s2s3.jsonl \
    --salida modelo/modelo_guard.npz
```
