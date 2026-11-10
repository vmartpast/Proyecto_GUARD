# Firmware del ESP32

Firmware de la interfaz de mando y aviso (`guard_interfaz.ino`), desarrollado
con el IDE de Arduino y el soporte del ESP32 (core 3.x) para la placa
Freenove ESP32-WROVER.

Funciones: lectura del joystick (envío de acciones `BTN` al panel), LEDs
verde, ámbar y rojo, zumbador con tono según la potencia recibida y
temporizador de enlace propio (estado `LINK_DOWN` tras 6 s sin latido).
El protocolo se especifica en `docs/PROTOCOLO_UART.md`.

| Función | GPIO |
|---|---|
| RX de UART2 (desde la Pi, GPIO 4) | 33 |
| TX de UART2 (hacia la Pi, GPIO 5) | 32 |
| Joystick, ejes X / Y (ADC1) | 34 / 35 |
| Pulsador del joystick | 25 |
| LEDs verde / ámbar / rojo | 4 / 5 / 18 |
| Zumbador (vía transistor S8050) | 19 |
