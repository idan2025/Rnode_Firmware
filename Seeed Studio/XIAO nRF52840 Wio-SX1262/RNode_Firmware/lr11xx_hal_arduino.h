#line 1 "/tmp/RNode_Firmware/lr11xx_hal_arduino.h"
// Arduino/RNode HAL glue for the vendored Semtech LR11xx driver.
// Copyright Mark Qvist. Licensed under the MIT license.

#ifndef LR11XX_HAL_ARDUINO_H
#define LR11XX_HAL_ARDUINO_H

#include <Arduino.h>
#include <SPI.h>

void lr11xx_hal_arduino_init(int ss_pin, int busy_pin, int reset_pin, SPIClass* spi, SPISettings spi_settings);

#endif
