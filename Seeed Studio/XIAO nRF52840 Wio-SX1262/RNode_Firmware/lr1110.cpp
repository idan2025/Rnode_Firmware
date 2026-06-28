#line 1 "/tmp/RNode_Firmware/lr1110.cpp"
// Copyright Mark Qvist.
// Licensed under the MIT license.

#include "Boards.h"

#if MODEM == LR1110
#include "lr1110.h"
#include "lr11xx_radio.h"
#include "lr11xx_system.h"
#include "lr11xx_regmem.h"
#include "lr11xx_hal.h"
#include "lr11xx_hal_arduino.h"

#define ISR_VECT

#define MAX_PKT_LENGTH 255
#define SYNC_WORD_PRIVATE 0x12
// The LR1110 has a single 256-byte RX/TX data buffer, addressed by 8-bit
// offsets (hence the uint8 buffer_start_pointer / read offsets). RX-continuous
// reception advances the write pointer through it and wraps at this boundary.
#define LR1110_RX_BUFFER_SIZE 256

// PA duty-cycle/hp_sel configs per requested dBm, sourced verbatim from
// Seeed's reference ral_lr11xx_bsp.c (LR11XX_PA_LP_LF_CFG_TABLE /
// LR11XX_PA_HP_LF_CFG_TABLE for the T1000E board). A single fixed
// duty_cycle/hp_sel pair is only correct for one specific dBm target -
// using it indiscriminately for any requested power level mis-biases
// the PA outside that one point, which can visibly degrade actual
// radiated power/range without showing up as a local TX failure.
#define LR1110_PA_LP_MIN_DBM -17
#define LR1110_PA_LP_MAX_DBM 15
#define LR1110_PA_HP_MIN_DBM -9
#define LR1110_PA_HP_MAX_DBM 22

// Extracted programmatically from the header above to avoid transcription
// errors - do not hand-edit. Index 0 = LR1110_PA_LP/HP_MIN_DBM.
static const uint8_t lr1110_pa_lp_duty_cycle[LR1110_PA_LP_MAX_DBM - LR1110_PA_LP_MIN_DBM + 1] = {
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x02, 0x03, 0x04, 0x07
};
static const uint8_t lr1110_pa_lp_hp_sel[LR1110_PA_LP_MAX_DBM - LR1110_PA_LP_MIN_DBM + 1] = {
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
};
static const uint8_t lr1110_pa_hp_duty_cycle[LR1110_PA_HP_MAX_DBM - LR1110_PA_HP_MIN_DBM + 1] = {
  0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x02, 0x04, 0x00, 0x00, 0x01, 0x02, 0x00, 0x04, 0x02,
  0x01, 0x04, 0x00, 0x01, 0x02, 0x03, 0x00, 0x01, 0x04, 0x01, 0x02, 0x01, 0x03, 0x03, 0x04, 0x04
};
static const uint8_t lr1110_pa_hp_hp_sel[LR1110_PA_HP_MAX_DBM - LR1110_PA_HP_MIN_DBM + 1] = {
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x02, 0x01, 0x02,
  0x03, 0x02, 0x01, 0x01, 0x01, 0x01, 0x03, 0x03, 0x02, 0x04, 0x04, 0x06, 0x05, 0x07, 0x06, 0x07
};

#if defined(NRF52840_XXAA)
  extern SPIClass spiModem;
  #define SPI spiModem
#endif
extern SPIClass SPI;

// LR1110 has no HAL "context" concept in this single-radio firmware,
// every driver call below passes NULL.
#define CTX NULL

lr1110::lr1110() :
  _spiSettings(4E6, MSBFIRST, SPI_MODE0),
  _ss(LORA_DEFAULT_SS_PIN), _reset(LORA_DEFAULT_RESET_PIN), _dio0(LORA_DEFAULT_DIO0_PIN), _busy(LORA_DEFAULT_BUSY_PIN), _rxen(LORA_DEFAULT_RXEN_PIN),
  _frequency(0),
  _txp(0),
  _sf(0x07),
  _bw(0x04),
  _cr(0x01),
  _ldro(0x00),
  _packetIndex(0),
  _preambleLength(18),
  _implicitHeaderMode(0),
  _payloadLength(255),
  _crcMode(1),
  _preinit_done(false),
  _onReceive(NULL)
{ setTimeout(0); }

bool lr1110::preInit() {
  pinMode(_ss, OUTPUT);
  digitalWrite(_ss, HIGH);
  SPI.begin();

  lr11xx_hal_arduino_init(_ss, _busy, _reset, &SPI, _spiSettings);

  reset();

  lr11xx_system_version_t version;
  long start = millis();
  bool found = false;
  while (((millis() - start) < 2000) && (millis() >= start)) {
    if (lr11xx_system_get_version(CTX, &version) == LR11XX_STATUS_OK) {
      if (version.type == LR11XX_SYSTEM_VERSION_TYPE_LR1110) { found = true; break; }
    }
    delay(100);
  }
  if (!found) { return false; }

  _preinit_done = true;
  return true;
}

void lr1110::waitOnBusy() {
  unsigned long time = millis();
  if (_busy != -1) {
    while (digitalRead(_busy) == HIGH) {
      if (millis() >= (time + 100)) { break; }
    }
  }
}

void lr1110::rxAntEnable() { }
void lr1110::loraMode() { lr11xx_radio_set_pkt_type(CTX, LR11XX_RADIO_PKT_TYPE_LORA); }

void lr1110::reset(void) {
  lr11xx_hal_reset(CTX);
}

void lr1110::calibrate(void) {
  lr11xx_system_calibrate(CTX,
    LR11XX_SYSTEM_CALIB_LF_RC_MASK | LR11XX_SYSTEM_CALIB_HF_RC_MASK | LR11XX_SYSTEM_CALIB_PLL_MASK |
    LR11XX_SYSTEM_CALIB_ADC_MASK  | LR11XX_SYSTEM_CALIB_IMG_MASK  | LR11XX_SYSTEM_CALIB_PLL_TX_MASK);
  delay(5);
}

int lr1110::begin(long frequency) {
  if (!_preinit_done) { if (!preInit()) { return false; } }

  // Proven init sequence, matches Semtech's reference RAL layer
  // (ral_lr11xx_init) and Seeed's board-specific RF switch / TCXO
  // configuration for this hardware.
  lr11xx_system_set_reg_mode(CTX, LR11XX_SYSTEM_REG_MODE_DCDC);

  lr11xx_system_rfswitch_cfg_t rfswitch_cfg;
  rfswitch_cfg.enable  = LR11XX_SYSTEM_RFSW0_HIGH | LR11XX_SYSTEM_RFSW1_HIGH | LR11XX_SYSTEM_RFSW2_HIGH | LR11XX_SYSTEM_RFSW3_HIGH;
  rfswitch_cfg.standby = 0;
  rfswitch_cfg.rx      = LR11XX_SYSTEM_RFSW0_HIGH;
  rfswitch_cfg.tx       = LR11XX_SYSTEM_RFSW0_HIGH | LR11XX_SYSTEM_RFSW1_HIGH;
  rfswitch_cfg.tx_hp    = LR11XX_SYSTEM_RFSW1_HIGH;
  rfswitch_cfg.tx_hf    = 0;
  rfswitch_cfg.gnss     = LR11XX_SYSTEM_RFSW2_HIGH;
  rfswitch_cfg.wifi     = LR11XX_SYSTEM_RFSW3_HIGH;
  lr11xx_system_set_dio_as_rf_switch(CTX, &rfswitch_cfg);

  enableTCXO();
  calibrate();

  loraMode();
  standby();

  setSyncWord(0);
  rxAntEnable();
  setFrequency(frequency);
  setTxPower(2);
  enableCrc();

  setModulationParams(_sf, _bw, _cr, _ldro);
  setPacketParams(_preambleLength, _implicitHeaderMode, _payloadLength, _crcMode);
  lr11xx_radio_cfg_rx_boosted(CTX, true);

  return 1;
}

void lr1110::end() { sleep(); SPI.end(); _preinit_done = false; }

int lr1110::beginPacket(int implicitHeader) {
  standby();
  if (implicitHeader) { implicitHeaderMode(); }
  else { explicitHeaderMode(); }

  _payloadLength = 0;
  return 1;
}

int lr1110::endPacket() {
  lr11xx_regmem_write_buffer8(CTX, _txbuf, (uint8_t)_payloadLength);
  setPacketParams(_preambleLength, _implicitHeaderMode, _payloadLength, _crcMode);

  lr11xx_system_clear_irq_status(CTX, LR11XX_SYSTEM_IRQ_ALL_MASK);
  lr11xx_radio_set_tx(CTX, 0);

  bool timed_out = false;
  uint32_t w_timeout = millis() + LORA_MODEM_TIMEOUT_MS;
  lr11xx_system_irq_mask_t irq = LR11XX_SYSTEM_IRQ_NONE;
  while (millis() < w_timeout) {
    lr11xx_system_get_and_clear_irq_status(CTX, &irq);
    if (irq & LR11XX_SYSTEM_IRQ_TX_DONE) { break; }
    yield();
  }
  if (!(irq & LR11XX_SYSTEM_IRQ_TX_DONE)) { timed_out = true; }

  if (timed_out) { return 0; } else { return 1; }
}

static unsigned long preamble_detected_at = 0;
extern long lora_preamble_time_ms;
extern long lora_header_time_ms;
static bool false_preamble_detected = false;

bool lr1110::dcd() {
  // Non-destructive read of the latched IRQ status. dcd() is polled from
  // the main loop (every STATUS_INTERVAL_MS) purely to drive channel-busy
  // / CSMA and the RX LED, concurrently with handleDio0Rise() servicing
  // the RX_DONE interrupt. We must NOT use the get-and-clear variant here:
  // it would race with the ISR and could erase a pending RX_DONE before
  // the handler sees it, dropping the packet payload.
  //
  // The LR11xx PREAMBLE_DETECTED / HEADER_VALID status bits stay latched
  // in the chip until explicitly cleared, so they can be read live on
  // every poll - exactly like sx126x::dcd(). Do NOT cache them in sticky
  // flags: with sticky latching, a single observed header keeps dcd()
  // asserting carrier forever (until the next RX_DONE), which pins the RX
  // LED on and makes CSMA see the medium as permanently busy, throttling
  // TX and stalling multi-packet transfers. When a preamble fails to
  // mature into a valid header within the expected window we clear ONLY
  // the preamble bit (clearing a single bit never disturbs a pending
  // RX_DONE) and re-arm RX, matching the proven sx126x path.
  lr11xx_system_irq_mask_t irq = LR11XX_SYSTEM_IRQ_NONE;
  lr11xx_system_get_irq_status(CTX, &irq);

  uint32_t now = millis();
  bool header_detected = false;
  bool carrier_detected = false;

  if (irq & LR11XX_SYSTEM_IRQ_SYNC_WORD_HEADER_VALID) { header_detected = true; carrier_detected = true; }

  if (irq & LR11XX_SYSTEM_IRQ_PREAMBLE_DETECTED) {
    carrier_detected = true;
    if (preamble_detected_at == 0) { preamble_detected_at = now; }
    if (now - preamble_detected_at > (unsigned long)(lora_preamble_time_ms + lora_header_time_ms)) {
      preamble_detected_at = 0;
      if (!header_detected) { false_preamble_detected = true; }
      lr11xx_system_clear_irq_status(CTX, LR11XX_SYSTEM_IRQ_PREAMBLE_DETECTED);
    }
  }

  if (false_preamble_detected) {
    lr1110_modem.receive();
    false_preamble_detected = false;
  }

  return carrier_detected;
}

uint8_t lr1110::currentRssiRaw() { return (uint8_t)(-currentRssi()); }

int ISR_VECT lr1110::currentRssi() {
  int8_t rssi = 0;
  lr11xx_radio_get_rssi_inst(CTX, &rssi);
  return (int)rssi;
}

uint8_t lr1110::packetRssiRaw() { return (uint8_t)(-packetRssi()); }

int ISR_VECT lr1110::packetRssi() {
  lr11xx_radio_pkt_status_lora_t status;
  lr11xx_radio_get_lora_pkt_status(CTX, &status);
  return (int)status.rssi_pkt_in_dbm;
}

int ISR_VECT lr1110::packetRssi(uint8_t pkt_snr_raw) { return packetRssi(); }

uint8_t ISR_VECT lr1110::packetSnrRaw() {
  lr11xx_radio_pkt_status_lora_t status;
  lr11xx_radio_get_lora_pkt_status(CTX, &status);
  // The KISS/host protocol always decodes this byte as (signed)*0.25,
  // matching the SX126x/SX127x raw SNR register encoding. The LR1110
  // driver instead reports SNR directly in dB, so it must be re-scaled
  // by 4 here to keep the wire format consistent across modem types.
  return (uint8_t)(int8_t)(status.snr_pkt_in_db * 4);
}

float ISR_VECT lr1110::packetSnr() {
  lr11xx_radio_pkt_status_lora_t status;
  lr11xx_radio_get_lora_pkt_status(CTX, &status);
  return (float)status.snr_pkt_in_db;
}

long lr1110::packetFrequencyError() { return 0; }

size_t lr1110::write(uint8_t byte) { return write(&byte, sizeof(byte)); }
size_t lr1110::write(const uint8_t *buffer, size_t size) {
  if (((size_t)_payloadLength + size) > MAX_PKT_LENGTH) { size = MAX_PKT_LENGTH - _payloadLength; }
  memcpy(_txbuf + _payloadLength, buffer, size);
  _payloadLength = _payloadLength + size;
  return size;
}

int ISR_VECT lr1110::available() {
  lr11xx_radio_rx_buffer_status_t rxbuf;
  lr11xx_radio_get_rx_buffer_status(CTX, &rxbuf);
  return (int)rxbuf.pld_len_in_bytes - _packetIndex;
}

// Copy the just-received packet from the LR1110's 256-byte RX data buffer into
// _packet, handling wrap. In RX-continuous mode the radio advances its write
// pointer through the buffer for each received packet, so buffer_start_pointer
// is rarely 0. lr11xx_regmem_read_buffer8 reads a LINEAR span and does NOT wrap
// at the 256-byte boundary, so a packet with start_pointer + pld_len > 256 has
// its tail read from out-of-bounds (garbage) unless we split the read at the
// wrap point. Small packets near offset 0 never wrap (why short messages work),
// but a large packet - e.g. the full ~255-byte first half of a split 380-char
// message - does, corrupting reassembly. (sx126x is immune: its FIFO read wraps
// in hardware.) Splitting into [start..end-of-buffer] + [0..remainder] is
// correct whether or not the chip wraps internally, since each sub-read stays
// within 0..256.
void lr1110::loadPacket() {
  lr11xx_radio_rx_buffer_status_t rxbuf;
  lr11xx_radio_get_rx_buffer_status(CTX, &rxbuf);
  uint16_t start = rxbuf.buffer_start_pointer;
  uint16_t len   = rxbuf.pld_len_in_bytes;
  if (len > MAX_PKT_LENGTH) { len = MAX_PKT_LENGTH; }

  if (start + len <= LR1110_RX_BUFFER_SIZE) {
    lr11xx_regmem_read_buffer8(CTX, _packet, (uint8_t)start, (uint8_t)len);
  } else {
    uint8_t first = (uint8_t)(LR1110_RX_BUFFER_SIZE - start);
    lr11xx_regmem_read_buffer8(CTX, _packet, (uint8_t)start, first);
    lr11xx_regmem_read_buffer8(CTX, _packet + first, 0, (uint8_t)(len - first));
  }
}

int ISR_VECT lr1110::read() {
  if (!available()) { return -1; }
  if (_packetIndex == 0) { loadPacket(); }

  uint8_t byte = _packet[_packetIndex];
  _packetIndex++;
  return byte;
}

int lr1110::peek() {
  if (!available()) { return -1; }
  if (_packetIndex == 0) {
    loadPacket();
  }
  return _packet[_packetIndex];
}

void lr1110::flush() { }

void lr1110::onDio0Rise() { lr1110_modem.handleDio0Rise(); }

void ISR_VECT lr1110::handleDio0Rise() {
  lr11xx_system_irq_mask_t irq = LR11XX_SYSTEM_IRQ_NONE;
  lr11xx_system_get_and_clear_irq_status(CTX, &irq);

  if (irq & LR11XX_SYSTEM_IRQ_RX_DONE) {
    // Completed packet: get_and_clear above already cleared the latched
    // preamble/header status bits, so just reset the false-preamble timer.
    preamble_detected_at = 0;
    if (!(irq & LR11XX_SYSTEM_IRQ_CRC_ERROR)) {
      _packetIndex = 0;
      lr11xx_radio_rx_buffer_status_t rxbuf;
      lr11xx_radio_get_rx_buffer_status(CTX, &rxbuf);
      if (_onReceive) { _onReceive(rxbuf.pld_len_in_bytes); }
    }
  }
}

void lr1110::onReceive(void(*callback)(int)) {
  _onReceive = callback;

  if (callback) {
    // The LR1110 IRQ line on this board needs an explicit pulldown
    // (confirmed against Seeed's own reference HAL, which configures
    // this exact pin with NRF_GPIO_PIN_PULLDOWN). Left floating, the
    // line can sit high from leakage/coupling, so the RISING-edge
    // interrupt for RX_DONE never fires and packets are never
    // delivered, even though TX (which only polls over SPI) works.
    pinMode(_dio0, INPUT_PULLDOWN);
    lr11xx_system_set_dio_irq_params(CTX, LR11XX_SYSTEM_IRQ_RX_DONE, LR11XX_SYSTEM_IRQ_NONE);
    #ifdef SPI_HAS_NOTUSINGINTERRUPT
      SPI.usingInterrupt(digitalPinToInterrupt(_dio0));
    #endif
    attachInterrupt(digitalPinToInterrupt(_dio0), lr1110::onDio0Rise, RISING);
  } else {
    detachInterrupt(digitalPinToInterrupt(_dio0));
    #ifdef SPI_HAS_NOTUSINGINTERRUPT
      SPI.notUsingInterrupt(digitalPinToInterrupt(_dio0));
    #endif
    lr11xx_system_set_dio_irq_params(CTX, LR11XX_SYSTEM_IRQ_NONE, LR11XX_SYSTEM_IRQ_NONE);
  }
}

void lr1110::receive(int size) {
  if (size > 0) {
    implicitHeaderMode();
    _payloadLength = size;
    setPacketParams(_preambleLength, _implicitHeaderMode, _payloadLength, _crcMode);
  } else {
    // Explicit-header RX: on the LR11xx the pld_len in the packet params acts as
    // the MAXIMUM accepted payload length - a received LoRa packet longer than
    // pld_len is silently dropped (never raises RX_DONE), unlike the sx126x where
    // explicit-header RX ignores pld_len. _payloadLength is left at the last TX
    // size by endPacket(), so without forcing it back to the maximum here, RX
    // silently rejects anything bigger than the last packet we transmitted (e.g.
    // an announce ~211 B), which is why large/split frames - the first ~255 B
    // half of a 380-char message - never arrived while short messages did. Pin
    // it to MAX_PKT_LENGTH so RX always accepts full-length packets.
    _payloadLength = MAX_PKT_LENGTH;
    explicitHeaderMode();
  }

  lr11xx_system_clear_irq_status(CTX, LR11XX_SYSTEM_IRQ_ALL_MASK);
  // RX continuous: the LR11xx timeout argument is NOT "0 == no timeout".
  // Per the datasheet/driver (lr11xx_radio.h), timeout 0x000000 = RX SINGLE
  // (receive one packet then drop to standby), and only 0xFFFFFF = RX
  // CONTINUOUS (stay in RX after each packet). RNode's main loop never
  // re-arms RX after a received packet (it relies on continuous mode, exactly
  // like sx126x.cpp which issues OP_RX with {0xFF,0xFF,0xFF}), so calling
  // set_rx(CTX, 0) here left the radio in single mode: it received exactly one
  // packet (the first half of a split announce), buffered it without
  // delivering, then went to standby - never receiving the second half or any
  // further packets. Use the raw rtc_step continuous sentinel 0xFFFFFF (the
  // ms->rtc_step conversion in set_rx would not yield exactly 0xFFFFFF).
  lr11xx_radio_set_rx_with_timeout_in_rtc_step(CTX, 0xFFFFFF);
}

void lr1110::standby() { lr11xx_system_set_standby(CTX, LR11XX_SYSTEM_STANDBY_CFG_RC); }
void lr1110::sleep() {
  lr11xx_system_sleep_cfg_t sleep_cfg = {0};
  lr11xx_system_set_sleep(CTX, sleep_cfg, 0);
}

void lr1110::enableTCXO() {
  // tick = 30.52 us (RTC freq 32768 Hz). Seeed's reference
  // (smtc_modem_hal_get_radio_tcxo_startup_delay_ms() = 30ms) waits
  // 30ms = 983 ticks for the TCXO to actually stabilize before any
  // calibration/RF operation runs. A too-short wait here lets the PLL
  // calibrate (and TX/RX proceed) against an unsettled clock reference,
  // which silently mistunes the carrier without failing any local status
  // check (TX_DONE still fires, frequency readback still looks right).
  lr11xx_system_set_tcxo_mode(CTX, LR11XX_SYSTEM_TCXO_CTRL_1_8V, 983);
}
void lr1110::disableTCXO() { }

void lr1110::setTxPower(int level, int outputPin) {
  if (level > LR1110_PA_HP_MAX_DBM) { level = LR1110_PA_HP_MAX_DBM; }
  else if (level < LR1110_PA_LP_MIN_DBM) { level = LR1110_PA_LP_MIN_DBM; }

  lr11xx_radio_pa_cfg_t pa_cfg;
  if (level <= LR1110_PA_LP_MAX_DBM) {
    pa_cfg.pa_sel = LR11XX_RADIO_PA_SEL_LP;
    pa_cfg.pa_reg_supply = LR11XX_RADIO_PA_REG_SUPPLY_VREG;
    pa_cfg.pa_duty_cycle = lr1110_pa_lp_duty_cycle[level - LR1110_PA_LP_MIN_DBM];
    pa_cfg.pa_hp_sel = lr1110_pa_lp_hp_sel[level - LR1110_PA_LP_MIN_DBM];
  } else {
    pa_cfg.pa_sel = LR11XX_RADIO_PA_SEL_HP;
    pa_cfg.pa_reg_supply = LR11XX_RADIO_PA_REG_SUPPLY_VBAT;
    pa_cfg.pa_duty_cycle = lr1110_pa_hp_duty_cycle[level - LR1110_PA_HP_MIN_DBM];
    pa_cfg.pa_hp_sel = lr1110_pa_hp_hp_sel[level - LR1110_PA_HP_MIN_DBM];
  }
  lr11xx_radio_set_pa_cfg(CTX, &pa_cfg);
  lr11xx_radio_set_tx_params(CTX, (int8_t)level, LR11XX_RADIO_RAMP_48_US);

  _txp = level;
}

uint8_t lr1110::getTxPower() { return _txp; }

void lr1110::setFrequency(long frequency) {
  _frequency = frequency;
  lr11xx_radio_set_rf_freq(CTX, (uint32_t)frequency);
}

uint32_t lr1110::getFrequency() { return (uint32_t)_frequency; }

void lr1110::setSpreadingFactor(int sf) {
  if (sf < 5) { sf = 5; }
  else if (sf > 12) { sf = 12; }
  _sf = sf;

  handleLowDataRate();
  setModulationParams(sf, _bw, _cr, _ldro);
}

long lr1110::getSignalBandwidth() {
  int bw = _bw;
  switch (bw) {
    case 0x00: return 7.8E3;
    case 0x01: return 15.6E3;
    case 0x02: return 31.25E3;
    case 0x03: return 62.5E3;
    case 0x04: return 125E3;
    case 0x05: return 250E3;
    case 0x06: return 500E3;
    case 0x08: return 10.4E3;
    case 0x09: return 20.8E3;
    case 0x0A: return 41.7E3;
  }
  return 0;
}

extern bool lora_low_datarate;
void lr1110::handleLowDataRate() {
  if (long((1 << _sf) / (getSignalBandwidth() / 1000)) > 16)
       { _ldro = 0x01; lora_low_datarate = true;  }
  else { _ldro = 0x00; lora_low_datarate = false; }
}

void lr1110::setSignalBandwidth(long sbw) {
  if      (sbw <= 7.8E3)   { _bw = 0x00; }
  else if (sbw <= 10.4E3)  { _bw = 0x08; }
  else if (sbw <= 15.6E3)  { _bw = 0x01; }
  else if (sbw <= 20.8E3)  { _bw = 0x09; }
  else if (sbw <= 31.25E3) { _bw = 0x02; }
  else if (sbw <= 41.7E3)  { _bw = 0x0A; }
  else if (sbw <= 62.5E3)  { _bw = 0x03; }
  else if (sbw <= 125E3)   { _bw = 0x04; }
  else if (sbw <= 250E3)   { _bw = 0x05; }
  else                     { _bw = 0x06; }

  handleLowDataRate();
  setModulationParams(_sf, _bw, _cr, _ldro);
}

void lr1110::setCodingRate4(int denominator) {
  if (denominator < 5) { denominator = 5; }
  else if (denominator > 8) { denominator = 8; }
  int cr = denominator - 4;
  _cr = cr;
  setModulationParams(_sf, _bw, cr, _ldro);
}

void lr1110::setPreambleLength(long preamble_symbols) {
  _preambleLength = preamble_symbols;
  setPacketParams(preamble_symbols, _implicitHeaderMode, _payloadLength, _crcMode);
}

void lr1110::setSyncWord(uint16_t sw) {
  // The LR1110's SetLoRaSyncWord command only accepts a single byte,
  // unlike the SX126x/SX127x family's two-register custom sync word.
  // RNode's usual 0x1424 custom value therefore cannot be replicated
  // bit-for-bit on this chip; the standard "private network" value is
  // used instead. Note: this means LR1110-based RNodes will not share
  // a PHY-level sync word with SX126x/SX127x-based RNodes on the same
  // frequency.
  lr11xx_radio_set_lora_sync_word(CTX, SYNC_WORD_PRIVATE);
}

void lr1110::setPins(int ss, int reset, int dio0, int busy, int rxen) {
  _ss = ss;
  _reset = reset;
  _dio0 = dio0;
  _busy = busy;
  _rxen = rxen;
}

void lr1110::setModulationParams(uint8_t sf, uint8_t bw, uint8_t cr, int ldro) {
  lr11xx_radio_mod_params_lora_t mod_params;
  mod_params.sf = (lr11xx_radio_lora_sf_t)sf;
  mod_params.bw = (lr11xx_radio_lora_bw_t)bw;
  mod_params.cr = (lr11xx_radio_lora_cr_t)cr;
  mod_params.ldro = (uint8_t)ldro;
  lr11xx_radio_set_lora_mod_params(CTX, &mod_params);
}

void lr1110::setPacketParams(long preamble_symbols, uint8_t headermode, uint8_t payload_length, uint8_t crc) {
  lr11xx_radio_pkt_params_lora_t pkt_params;
  pkt_params.preamble_len_in_symb = (uint16_t)preamble_symbols;
  pkt_params.header_type = headermode ? LR11XX_RADIO_LORA_PKT_IMPLICIT : LR11XX_RADIO_LORA_PKT_EXPLICIT;
  pkt_params.pld_len_in_bytes = payload_length;
  pkt_params.crc = crc ? LR11XX_RADIO_LORA_CRC_ON : LR11XX_RADIO_LORA_CRC_OFF;
  pkt_params.iq = LR11XX_RADIO_LORA_IQ_STANDARD;
  lr11xx_radio_set_lora_pkt_params(CTX, &pkt_params);
}

void lr1110::explicitHeaderMode() { _implicitHeaderMode = 0; setPacketParams(_preambleLength, _implicitHeaderMode, _payloadLength, _crcMode); }
void lr1110::implicitHeaderMode() { _implicitHeaderMode = 1; setPacketParams(_preambleLength, _implicitHeaderMode, _payloadLength, _crcMode); }
void lr1110::enableCrc() { _crcMode = 1; setPacketParams(_preambleLength, _implicitHeaderMode, _payloadLength, _crcMode); }
void lr1110::disableCrc() { _crcMode = 0; setPacketParams(_preambleLength, _implicitHeaderMode, _payloadLength, _crcMode); }
void lr1110::setSPIFrequency(uint32_t frequency) { _spiSettings = SPISettings(frequency, MSBFIRST, SPI_MODE0); }

byte lr1110::random() {
  uint32_t r = 0;
  lr11xx_system_get_random_number(CTX, &r);
  return (byte)(r & 0xFF);
}

lr1110 lr1110_modem;

#endif
