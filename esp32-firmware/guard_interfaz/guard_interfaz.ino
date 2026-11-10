// guard_interfaz: firmware del ESP32 que hace de interfaz fisica de GUARD.
// Recibe por la UART los latidos y detecciones de la Raspberry Pi, avisa con
// LEDs y zumbador, y envia al panel las pulsaciones del joystick.
//
// Tramas que recibe (Pi -> ESP32):
//   HB|uptime|estado|      latido; estado OK, DEGRADED o ERROR
//   DET|rssi|modelo|conf|  deteccion confirmada (ya filtrada por la
//                          histeresis del puente)
//   SYS|cpu|temp|mem|      telemetria (se acepta y no se usa)
// Tramas que envia (ESP32 -> Pi): BTN|accion|, ACK|DET|, ERR|codigo|

#define PIN_RX      33   // recibe de la Pi (su GPIO 4, pin fisico 7)
#define PIN_TX      32   // transmite a la Pi (su GPIO 5, pin fisico 29)
#define LED_VERDE   4
#define LED_AMBAR   5
#define LED_ROJO    18
#define BUZZER      19   // via transistor S8050
#define JOY_X       34   // ADC1: el ADC2 deja de funcionar con WiFi activo
#define JOY_Y       35
#define JOY_SW      25   // digital con pull-up interno (34-39 no lo tienen)

const uint32_t BAUD_ENLACE     = 115200;
const uint32_t TIMEOUT_LINK    = 6000;   // 3 latidos perdidos -> LINK_DOWN
const uint32_t DURACION_ALERTA = 10000;  // sin nuevas DET -> estado base
const uint8_t  MAX_TRAMA       = 96;

// Tono segun potencia recibida: mas cerca, mas agudo.
const int RSSI_MIN   = -85;
const int RSSI_MAX   = -30;
const int TONO_GRAVE = 1600;
const int TONO_AGUDO = 2600;
const int PITIDO_ON  = 220;
const int PITIDO_OFF = 120;
const int PITIDOS    = 3;

// Joystick. El eje Y queda invertido por la orientacion del modulo.
const bool INVERTIR_X  = false;
const bool INVERTIR_Y  = true;
const int  MUESTRAS    = 8;     // media movil: el ADC tiene +-50-100 de ruido
const int  ZONA_MUERTA = 800;   // holgado: solo hacen falta 4 direcciones
const uint32_t REPETICION_MS  = 350;  // al mantener, solo arriba/abajo
const uint32_t ANTIRREBOTE_MS = 40;

// Estado
enum Estado { BOOT, LINK_DOWN, IDLE, DEGRADED, ALERT };
enum Direccion { D_CENTRO, D_ARRIBA, D_ABAJO, D_IZQUIERDA, D_DERECHA };

Estado   estado       = BOOT;
Estado   estadoBase   = LINK_DOWN;   // estado segun el ultimo latido
uint32_t ultimoLatido = 0;           // 0 = todavia ningun latido
uint32_t ultimaDET    = 0;

// Zumbador sin bloqueo: secuencia de pitidos atendida en loop(), para no
// dejar de leer el enlace ni el joystick mientras suena.
int      pitidosPendientes = 0;
int      tonoPitido        = 0;
bool     sonando           = false;
uint32_t cambioZumbador    = 0;

// Joystick
int       centroX = 2048, centroY = 2048;
Direccion ultimaDir = D_CENTRO;
uint32_t  ultimoMovimiento = 0;
bool      swEstable = false, swLeido = false;
uint32_t  cambioSW = 0;

char    buffer[MAX_TRAMA];
uint8_t largoBuffer = 0;

// Tramas

/**
 * Calcula el XOR de los caracteres de un tramo de la trama, que es la
 * suma de control del protocolo.
 *
 * @param  s      trama recibida
 * @param  desde  posicion del primer caracter incluido
 * @param  hasta  posicion del primer caracter excluido
 * @return suma de control de un byte
 */
uint8_t calcularChecksum(const char* s, uint8_t desde, uint8_t hasta) {
  uint8_t cs = 0;
  for (uint8_t i = desde; i < hasta; i++) cs ^= (uint8_t)s[i];
  return cs;
}

/**
 * Envia una trama a la Pi con su delimitador, su suma de control y el
 * fin de linea.
 *
 * @param  cuerpo  campos de la trama, sin delimitadores
 */
void enviarTrama(const String& cuerpo) {
  uint8_t cs = 0;
  for (size_t i = 0; i < cuerpo.length(); i++) cs ^= (uint8_t)cuerpo[i];
  char hex[3];
  snprintf(hex, sizeof(hex), "%02X", cs);
  Serial2.print('<');
  Serial2.print(cuerpo);
  Serial2.print('*');
  Serial2.print(hex);
  Serial2.print('\n');
}

/**
 * Envia a la Pi una trama de error.
 *
 * @param  codigo  codigo de error (FMT, CS o LEN)
 */
void enviarError(const char* codigo) {
  enviarTrama(String("ERR|") + codigo + "|");
}

// Zumbador

/**
 * Programa una secuencia de pitidos, que atenderZumbador reproduce sin
 * bloquear el bucle principal.
 *
 * @param  tono   frecuencia del pitido en Hz
 * @param  veces  numero de pitidos
 */
void pitar(int tono, int veces) {
  tonoPitido = tono;
  pitidosPendientes = veces;
  sonando = false;
  cambioZumbador = 0;          // empieza en la siguiente vuelta de loop()
}

/**
 * Silencia el zumbador y cancela los pitidos pendientes.
 */
void callar() {
  pitidosPendientes = 0;
  sonando = false;
  ledcWriteTone(BUZZER, 0);
}

/**
 * Avanza la secuencia de pitidos: enciende o apaga el zumbador cuando
 * ha pasado el tiempo de cada fase.
 *
 * @param  ahora  instante actual en ms (millis)
 */
void atenderZumbador(uint32_t ahora) {
  if (sonando) {
    if (ahora - cambioZumbador >= (uint32_t)PITIDO_ON) {
      ledcWriteTone(BUZZER, 0);
      sonando = false;
      cambioZumbador = ahora;
    }
  } else if (pitidosPendientes > 0 &&
             (cambioZumbador == 0 || ahora - cambioZumbador >= PITIDO_OFF)) {
    ledcWriteTone(BUZZER, tonoPitido);
    sonando = true;
    pitidosPendientes--;
    cambioZumbador = ahora;
  }
}

// Estado y LEDs

/**
 * Enciende el LED que corresponde al estado: verde en IDLE, ambar en
 * DEGRADED y rojo en ALERT o LINK_DOWN.
 */
void aplicarLeds() {
  digitalWrite(LED_VERDE, estado == IDLE);
  digitalWrite(LED_AMBAR, estado == DEGRADED);
  digitalWrite(LED_ROJO,  estado == ALERT || estado == LINK_DOWN);
}

/**
 * Cambia el estado del dispositivo, actualiza los LEDs y silencia el
 * zumbador si se sale de la alerta.
 *
 * @param  nuevo  estado de destino
 */
void cambiarEstado(Estado nuevo) {
  if (estado == nuevo) return;
  estado = nuevo;
  if (estado != ALERT) callar();
  aplicarLeds();
  Serial.printf("[estado] -> %d\n", (int)nuevo);
}

// Parser

/**
 * Procesa un latido de la Pi: renueva el temporizador de enlace y fija
 * el estado base segun el estado que informa.
 *
 * @param  campos  campos de la trama tras el tipo (uptime|estado)
 */
void procesarHB(const String& campos) {
  int sep = campos.indexOf('|');
  if (sep < 0) { enviarError("FMT"); return; }
  String est = campos.substring(sep + 1);
  est.trim();

  if (est == "OK")                          estadoBase = IDLE;
  else if (est == "DEGRADED" || est == "ERROR") estadoBase = DEGRADED;
  else { enviarError("FMT"); return; }

  ultimoLatido = millis();
  if (estado != ALERT) cambiarEstado(estadoBase);
}

/**
 * Procesa una deteccion: responde con ACK, pasa a ALERT y hace sonar
 * el zumbador con un tono segun la potencia recibida.
 *
 * @param  campos  campos de la trama tras el tipo (rssi|modelo|conf)
 */
void procesarDET(const String& campos) {
  int p1 = campos.indexOf('|');
  int p2 = campos.indexOf('|', p1 + 1);
  if (p1 < 0 || p2 < 0) { enviarError("FMT"); return; }

  String sRssi  = campos.substring(0, p1);
  String modelo = campos.substring(p1 + 1, p2);

  int tono = (TONO_GRAVE + TONO_AGUDO) / 2;
  if (sRssi != "-" && sRssi.length() > 0) {
    int r = constrain((int)sRssi.toFloat(), RSSI_MIN, RSSI_MAX);
    tono = map(r, RSSI_MIN, RSSI_MAX, TONO_GRAVE, TONO_AGUDO);
  }

  enviarTrama("ACK|DET|");     // permite a la Pi medir la latencia
  ultimaDET = millis();

  // Sin enlace no se presenta la alerta: LINK_DOWN tiene prioridad.
  if (estado == LINK_DOWN) return;
  cambiarEstado(ALERT);
  pitar(tono, PITIDOS);
  Serial.printf("[alerta] %s\n", modelo.c_str());
}

/**
 * Valida una trama completa (delimitador y suma de control) y la reparte
 * segun su tipo.
 *
 * @param  t      trama recibida, terminada en nulo
 * @param  largo  longitud de la trama
 */
void procesarTrama(char* t, uint8_t largo) {
  if (t[0] != '>') return;

  int posCS = -1;
  for (int i = largo - 1; i > 0; i--) {
    if (t[i] == '*') { posCS = i; break; }
  }
  if (posCS < 0 || largo - posCS < 3) { enviarError("FMT"); return; }

  uint8_t esperado = calcularChecksum(t, 1, posCS);
  uint8_t recibido = (uint8_t)strtol(t + posCS + 1, nullptr, 16);
  if (esperado != recibido) { enviarError("CS"); return; }

  String cuerpo = String(t).substring(1, posCS);
  // El protocolo cierra la lista de campos con un separador final.
  if (cuerpo.endsWith("|")) cuerpo.remove(cuerpo.length() - 1);

  int sep = cuerpo.indexOf('|');
  if (sep < 0) { enviarError("FMT"); return; }
  String tipo  = cuerpo.substring(0, sep);
  String resto = cuerpo.substring(sep + 1);

  if (tipo == "HB")       procesarHB(resto);
  else if (tipo == "DET") procesarDET(resto);
  else if (tipo == "SYS") { /* telemetria: no se presenta */ }
  else                    enviarError("FMT");
}

/**
 * Lee los caracteres disponibles en la UART y procesa cada trama al
 * llegar el fin de linea.
 */
void leerEnlace() {
  while (Serial2.available()) {
    char c = Serial2.read();
    if (c == '\n') {
      if (largoBuffer > 0) {
        buffer[largoBuffer] = '\0';
        procesarTrama(buffer, largoBuffer);
        largoBuffer = 0;
      }
      continue;
    }
    if (c == '\r') continue;   // la Pi puede terminar con CRLF
    if (largoBuffer >= MAX_TRAMA - 1) {
      largoBuffer = 0;          // resincronizar: trama sobredimensionada
      enviarError("LEN");
      continue;
    }
    buffer[largoBuffer++] = c;
  }
}

// Joystick

/**
 * Lee un eje del joystick como media de varias muestras del ADC.
 *
 * @param  pin       pin analogico del eje
 * @param  invertir  true para invertir el sentido del eje
 * @return valor del eje entre 0 y 4095
 */
int leerEje(uint8_t pin, bool invertir) {
  long suma = 0;
  for (int i = 0; i < MUESTRAS; i++) suma += analogRead(pin);
  int v = suma / MUESTRAS;
  return invertir ? (4095 - v) : v;
}

/**
 * Toma la posicion de reposo del joystick como centro de los dos ejes.
 */
void calibrarJoystick() {
  long sx = 0, sy = 0;
  for (int i = 0; i < 32; i++) {
    sx += leerEje(JOY_X, INVERTIR_X);
    sy += leerEje(JOY_Y, INVERTIR_Y);
    delay(8);
  }
  centroX = sx / 32;
  centroY = sy / 32;
  Serial.printf("Joystick centrado en X=%d Y=%d\n", centroX, centroY);
}

/**
 * Convierte la posicion del joystick en una de las cuatro direcciones,
 * con una zona muerta alrededor del centro.
 *
 * @return direccion pulsada, o D_CENTRO en reposo
 */
Direccion leerDireccion() {
  int x = leerEje(JOY_X, INVERTIR_X);
  int y = leerEje(JOY_Y, INVERTIR_Y);
  if (y > centroY + ZONA_MUERTA) return D_ARRIBA;
  if (y < centroY - ZONA_MUERTA) return D_ABAJO;
  if (x > centroX + ZONA_MUERTA) return D_DERECHA;
  if (x < centroX - ZONA_MUERTA) return D_IZQUIERDA;
  return D_CENTRO;
}

/**
 * Envia al panel de la Pi una trama BTN con la accion pulsada.
 *
 * @param  accion  accion (arriba, abajo, atras u ok)
 */
void enviarAccion(const char* accion) {
  enviarTrama(String("BTN|") + accion + "|");
}

/**
 * Traduce una direccion del joystick a su accion y la envia. La derecha
 * no tiene accion, para evitar confirmaciones involuntarias.
 *
 * @param  d  direccion pulsada
 */
void enviarDireccion(Direccion d) {
  // Derecha no tiene accion: un empujon lateral involuntario no debe
  // confirmar nada en el panel.
  if (d == D_ARRIBA)         enviarAccion("arriba");
  else if (d == D_ABAJO)     enviarAccion("abajo");
  else if (d == D_IZQUIERDA) enviarAccion("atras");
}

/**
 * Detecta los movimientos y pulsaciones del joystick y los envia a la Pi.
 * En alerta, el pulsador silencia el zumbador sin esperar a la Pi.
 *
 * @param  ahora  instante actual en ms (millis)
 */
void atenderJoystick(uint32_t ahora) {
  Direccion d = leerDireccion();
  if (d != ultimaDir) {
    ultimaDir = d;
    ultimoMovimiento = ahora;
    if (d != D_CENTRO) enviarDireccion(d);
  } else if ((d == D_ARRIBA || d == D_ABAJO) &&
             ahora - ultimoMovimiento > REPETICION_MS) {
    ultimoMovimiento = ahora;   // repeticion al mantener
    enviarDireccion(d);
  }

  // Pulsador con antirrebote: el cambio debe mantenerse 40 ms.
  bool sw = (digitalRead(JOY_SW) == LOW);
  if (sw != swLeido) { swLeido = sw; cambioSW = ahora; }
  if (swLeido != swEstable && ahora - cambioSW >= ANTIRREBOTE_MS) {
    swEstable = swLeido;
    if (swEstable) {
      // Reconocimiento local: en alerta, el pulsador silencia el
      // zumbador sin esperar a la Pi. La accion se envia igualmente
      // al panel.
      if (estado == ALERT) callar();
      enviarAccion("ok");
    }
  }
}

// Setup

/**
 * Configura la UART, los pines, el zumbador y el joystick, y arranca en
 * LINK_DOWN hasta recibir el primer latido.
 */
void setup() {
  Serial.begin(115200);
  Serial2.begin(BAUD_ENLACE, SERIAL_8N1, PIN_RX, PIN_TX);

  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_AMBAR, OUTPUT);
  pinMode(LED_ROJO, OUTPUT);
  pinMode(JOY_SW, INPUT_PULLUP);
  ledcAttach(BUZZER, 2000, 8);
  ledcWriteTone(BUZZER, 0);

  Serial.println("\nGUARD — interfaz fisica iniciada");
  Serial.printf("Enlace: GPIO%d RX / GPIO%d TX @ %lu\n",
                PIN_RX, PIN_TX, BAUD_ENLACE);
  calibrarJoystick();

  // Sin latidos todavia: el estado de partida es enlace perdido.
  cambiarEstado(LINK_DOWN);
}

// Loop

/**
 * Bucle principal: lee el enlace y el joystick, gestiona el fin de la
 * alerta y la perdida de enlace, y atiende el zumbador.
 */
void loop() {
  leerEnlace();
  uint32_t ahora = millis();
  atenderJoystick(ahora);

  // Fin de la alerta: 10 s sin nuevas detecciones.
  if (estado == ALERT && ahora - ultimaDET > DURACION_ALERTA) {
    cambiarEstado(estadoBase);
  }

  // Perdida de enlace. Tiene prioridad sobre cualquier otro estado,
  // incluida una alerta en curso, y solo se abandona con un latido valido.
  if (ultimoLatido == 0 || ahora - ultimoLatido > TIMEOUT_LINK) {
    cambiarEstado(LINK_DOWN);
  }

  atenderZumbador(ahora);
  delay(10);
}
