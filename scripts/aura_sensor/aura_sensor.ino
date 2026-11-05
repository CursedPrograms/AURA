// aura_sensor.ino - AURA's own sensor: an AHT10 (temperature + humidity) on an Arduino over USB.
//
// The same AHT10 and the same reading code as MILA, so AURA's numbers and MILA's agree.
// Wiring (UNO / Nano): AHT10 VIN -> 5V (or 3.3V), GND -> GND, SDA -> A4, SCL -> A5.
//
// Serial, 9600 baud, one line each:
//   TEMP:21.4      every 5 s, degrees C
//   HUM:48.2       every 5 s, % relative humidity
//   AHT_ERROR      when the sensor doesn't answer (checked again every 5 s)
// Send "WHO" and she answers "I am AURA", which is how aura.py finds her port.
#include <Wire.h>

#define AHT10_ADDR 0x38
const unsigned long ENV_INTERVAL_MS = 5000;
const long BAUD = 9600;

String inLine;

void initAHT10() {
  Wire.beginTransmission(AHT10_ADDR);
  Wire.write(0x71);
  Wire.endTransmission();
  Wire.requestFrom(AHT10_ADDR, 1);
  uint8_t status = Wire.available() ? Wire.read() : 0;

  if ((status & 0x68) != 0x08) {  // calibration bit not set: (re)initialise
    Wire.beginTransmission(AHT10_ADDR);
    Wire.write(0xE1);
    Wire.write(0x08);
    Wire.write(0x00);
    Wire.endTransmission();
    delay(10);
  }
}

void readAHT10() {
  Wire.beginTransmission(AHT10_ADDR);
  Wire.write(0xAC);
  Wire.write(0x33);
  Wire.write(0x00);
  if (Wire.endTransmission() != 0) {  // sensor not responding
    Serial.println("AHT_ERROR");
    initAHT10();
    return;
  }

  delay(80);  // the AHT10 needs ~75 ms to measure

  Wire.requestFrom(AHT10_ADDR, 6);
  if (Wire.available() < 6) {
    Serial.println("AHT_ERROR");
    return;
  }
  uint8_t buf[6];
  for (int i = 0; i < 6; i++) buf[i] = Wire.read();
  if (buf[0] & 0x80) return;  // still busy: next time

  uint32_t rawHum = ((uint32_t)buf[1] << 12) | ((uint32_t)buf[2] << 4) | (buf[3] >> 4);
  uint32_t rawTemp = ((uint32_t)(buf[3] & 0x0F) << 16) | ((uint32_t)buf[4] << 8) | buf[5];

  Serial.print("TEMP:");
  Serial.println((rawTemp / 1048576.0) * 200.0 - 50.0, 1);
  Serial.print("HUM:");
  Serial.println((rawHum / 1048576.0) * 100.0, 1);
}

void handleCommand(const String &cmd) {
  if (cmd == "WHO") Serial.println("I am AURA");
  else if (cmd == "READ") readAHT10();      // a reading now, without waiting
}

void setup() {
  Serial.begin(BAUD);
  Wire.begin();
  delay(40);  // the AHT10 needs 20 ms after power-up
  initAHT10();
  Serial.println("I am AURA");
}

void loop() {
  static unsigned long lastEnvMs = 0;
  if (millis() - lastEnvMs >= ENV_INTERVAL_MS) {
    lastEnvMs = millis();
    readAHT10();
  }
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      inLine.trim();
      if (inLine.length()) handleCommand(inLine);
      inLine = "";
    } else if (inLine.length() < 32) {
      inLine += c;
    }
  }
}
