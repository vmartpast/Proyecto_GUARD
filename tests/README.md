# Pruebas de GUARD

Batería de pruebas unitarias, de integración y de sistema del dispositivo.
Se ejecuta en la Raspberry Pi desde la raíz del repositorio (`/opt/guard_device`).

| Fichero | Contenido |
|---|---|
| `ejecutar_pruebas.py` | Ejecuta todas las pruebas y genera el informe |
| `unitarias.py` | U1–U5: persistencia, detector de energía, lista blanca y tramas del puente. Sin hardware |
| `integracion.py` | I1–I5: tramas al ESP32, estado del detector, lista blanca en el servicio, acceso exclusivo al receptor y cadena de arranque |
| `sistema.py` | S1–S6: watchdogs, detector bloqueado y terminado, receptor desconectado, latencia de la alerta y cuelgue del sistema (manual) |
| `resultados/` | Informes `.txt` de cada ejecución e `historico.txt` con todos |

## Uso

```
cd /opt/guard_device
python3 tests/ejecutar_pruebas.py --solo-unitarias     # sin hardware
sudo python3 tests/ejecutar_pruebas.py                 # todas
sudo python3 tests/ejecutar_pruebas.py --sin-usb       # todas menos S4
sudo python3 tests/ejecutar_pruebas.py --solo I1 S2    # solo las indicadas
```

Las pruebas de integración y de sistema paran y reinician servicios, bloquean
el proceso del detector y desconectan el HackRF por software. Al terminar
dejan los servicios en marcha y la lista blanca como estaba.
