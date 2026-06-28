#line 1 "/tmp/RNode_Firmware/lr11xx_hal_arduino.cpp"
// Arduino/RNode HAL glue for the vendored Semtech LR11xx driver.
// Copyright Mark Qvist. Licensed under the MIT license.
//
// Implements the lr11xx_hal_* contract (lr11xx_hal.h) on top of the
// Arduino SPI API. RNode only ever drives a single LR11xx radio, so
// the pin/SPI configuration is kept in module-static state rather
// than threaded through the HAL "context" pointer.

#include "lr11xx_hal_arduino.h"
#include "lr11xx_hal.h"

static int _ss_pin = -1;
static int _busy_pin = -1;
static int _reset_pin = -1;
static SPIClass* _spi = NULL;
static SPISettings _spi_settings(4E6, MSBFIRST, SPI_MODE0);

void lr11xx_hal_arduino_init(int ss_pin, int busy_pin, int reset_pin, SPIClass* spi, SPISettings spi_settings) {
  _ss_pin = ss_pin;
  _busy_pin = busy_pin;
  _reset_pin = reset_pin;
  _spi = spi;
  _spi_settings = spi_settings;

  pinMode(_ss_pin, OUTPUT);
  digitalWrite(_ss_pin, HIGH);
  if (_busy_pin != -1) { pinMode(_busy_pin, INPUT); }
  if (_reset_pin != -1) { pinMode(_reset_pin, OUTPUT); digitalWrite(_reset_pin, HIGH); }
}

static void lr11xx_hal_wait_on_busy() {
  if (_busy_pin == -1) { return; }
  unsigned long start = millis();
  while (digitalRead(_busy_pin) == HIGH) {
    if (millis() - start > 1000) { break; }
  }
}

extern "C" lr11xx_hal_status_t lr11xx_hal_write(const void* context, const uint8_t* command, const uint16_t command_length,
                                                 const uint8_t* data, const uint16_t data_length) {
  lr11xx_hal_wait_on_busy();

  digitalWrite(_ss_pin, LOW);
  _spi->beginTransaction(_spi_settings);
  for (uint16_t i = 0; i < command_length; i++) { _spi->transfer(command[i]); }
  for (uint16_t i = 0; i < data_length; i++) { _spi->transfer(data[i]); }
  _spi->endTransaction();
  digitalWrite(_ss_pin, HIGH);

  // The chip raises BUSY while it processes most commands (including
  // GoToSleep). Waiting here keeps every subsequent HAL call honest
  // about chip readiness without each call needing to know which
  // commands are slow.
  lr11xx_hal_wait_on_busy();

  return LR11XX_HAL_STATUS_OK;
}

extern "C" lr11xx_hal_status_t lr11xx_hal_read(const void* context, const uint8_t* command, const uint16_t command_length,
                                                uint8_t* data, const uint16_t data_length) {
  lr11xx_hal_wait_on_busy();

  digitalWrite(_ss_pin, LOW);
  _spi->beginTransaction(_spi_settings);
  for (uint16_t i = 0; i < command_length; i++) { _spi->transfer(command[i]); }
  _spi->endTransaction();
  digitalWrite(_ss_pin, HIGH);

  lr11xx_hal_wait_on_busy();

  digitalWrite(_ss_pin, LOW);
  _spi->beginTransaction(_spi_settings);
  _spi->transfer(LR11XX_NOP); // discarded status/dummy byte
  for (uint16_t i = 0; i < data_length; i++) { data[i] = _spi->transfer(LR11XX_NOP); }
  _spi->endTransaction();
  digitalWrite(_ss_pin, HIGH);

  return LR11XX_HAL_STATUS_OK;
}

extern "C" lr11xx_hal_status_t lr11xx_hal_direct_read(const void* context, uint8_t* data, const uint16_t data_length) {
  lr11xx_hal_wait_on_busy();

  digitalWrite(_ss_pin, LOW);
  _spi->beginTransaction(_spi_settings);
  for (uint16_t i = 0; i < data_length; i++) { data[i] = _spi->transfer(LR11XX_NOP); }
  _spi->endTransaction();
  digitalWrite(_ss_pin, HIGH);

  return LR11XX_HAL_STATUS_OK;
}

extern "C" lr11xx_hal_status_t lr11xx_hal_reset(const void* context) {
  if (_reset_pin != -1) {
    digitalWrite(_reset_pin, LOW);
    delay(1);
    digitalWrite(_reset_pin, HIGH);
  }
  delay(150); // LR11xx boot time after reset
  lr11xx_hal_wait_on_busy();
  return LR11XX_HAL_STATUS_OK;
}

extern "C" lr11xx_hal_status_t lr11xx_hal_wakeup(const void* context) {
  digitalWrite(_ss_pin, LOW);
  delayMicroseconds(20);
  digitalWrite(_ss_pin, HIGH);
  lr11xx_hal_wait_on_busy();
  return LR11XX_HAL_STATUS_OK;
}
