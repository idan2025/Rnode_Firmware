# RNode Firmware for Seeed XIAO nRF52840 + Wio-SX1262 — Project State

Last updated: 2026-06-21.

## Current Status: BLE re-enabled (PENDING HW verify of the single-enable fix)

2026-06-22: BLE was re-enabled (HAS_BLE=true in the BOARD_XIAO_NRF52 block).
The earlier `bt_begin_early()` did `sd_softdevice_disable()` then
`Bluefruit.begin()` (re-enable). That **hangs at boot, blue-only LED** — the
re-enable wedges inside `sd_softdevice_enable` (before the green "SD enabled"
marker in bluefruit.cpp::begin). Disable-alone is safe (BLE-off path uses it),
but disable->enable hangs.

Fix applied 2026-06-22: removed the `sd_softdevice_disable()` from
`bt_begin_early()`. The adafruit nRF52 bootloader hands off with the SoftDevice
DISABLED (clean) — same as every adafruit nRF52 board and what Meshtastic
assumes on this exact XIAO. `Bluefruit.begin()` now does ONE clean enable.
**Awaiting HW flash + LED report to confirm** (expect: green SD-enabled marker
lights, then steady green + BLE advertises "RNode XXXX").

### The boot hang had THREE independent causes, found in order by LED bisection
1. **Flagfile block reused the global EEPROM `file` handle** (closed it), so the
   next eeprom_read() faulted. Fixed: deleted block, eeprom_begin() self-heals.
2. **Bluefruit.begin() (SoftDevice enable) hangs on this board WHEN preceded by
   `sd_softdevice_disable()`.** Hang sits at the re-enable — blue-only LED,
   before the green "SD enabled" marker in `bluefruit.cpp::begin`. Disable-alone
   is safe (BLE-off path uses it); disable→enable wedges. Fix: drop the
   pre-disable, let `Bluefruit.begin()` do one clean enable from the adafruit
   bootloader's SD-off handoff (Meshtastic path). Pending HW verify.
3. **InternalFS flash blocked forever even with BLE off.** The adafruit bootloader
   hands off with the SoftDevice ENABLED; the flash HAL (flash_nrf5x.c) only uses
   sd_flash_* and then waits on a completion event that only the (now absent)
   Bluefruit task dispatches -> infinite wait. Fixed with TWO changes:
   - setup() calls `sd_softdevice_disable()` before eeprom_begin() (BLE-off path).
   - **flash_nrf5x.c is PATCHED** to drive the NVMC directly when the SoftDevice
     is off (functions `rnode_sd_enabled` / `rnode_nvmc_erase` / `rnode_nvmc_write`).
     This patch lives in the bundled adafruit core
     (`.../1.7.0/libraries/InternalFileSytem/src/flash/flash_nrf5x.c`). The build
     script guards its presence; if the core is reinstalled, re-apply it.

### LED checkpoint map (markers since removed; kept here for reference)
1 setup entered · 2 get_rng_seed · 3 LoRa preInit · 4 Bluefruit.begin (BLE only)
· 5 eeprom_begin · 6 bt_init (BLE only) · 7 validate_status · then steady green.

(historical) Firmware compiles clean (adafruit core); DFU zip builds.

### Root cause (confirmed by code analysis)
The flagfile block in `setup()` reused the GLOBAL `file` handle that
`eeprom_begin()` owns for the EEPROM file:
```
eeprom_begin();              // global `file` open on "eeprom"
if (needs_format) {
  file.close();              // closes the eeprom handle
  file.open("/xiao_ok"...);  // hijacks it
  file.close();              // leaves global `file` CLOSED
}
bt_init();                   // bt_setup_hw -> eeprom_read() on a CLOSED file -> fault
```
Every `eeprom_read()/eeprom_update()` uses that global handle; the first access
after the block (in `bt_setup_hw`, then `validate_status`) operated on a closed
file → silent crash (no LED/USB). Matches the observed symptom exactly.

The flagfile was also redundant: `eeprom_begin()` already self-heals
(`InternalFS.begin()` auto-formats an unmountable FS on the adafruit core,
creates the file on first boot, reformats on a garbage/stale file) AND persists
config across reboots (config lives at file offset 96-295; the first-byte format
heuristic reads offset 0, which is unused padding = always 0xFF, so it never
wrongly wipes a provisioned device).

### Fix shipped
- `RNode_Firmware.ino.cpp` setup(): deleted the flagfile block, replaced with a
  bare `eeprom_begin();`.
- `build_xiao_adafruit.sh`: now writes `/tmp/acli_ada.yaml` (it never did) so
  arduino-cli finds the bundled adafruit core instead of "platform not installed".

### Known cosmetic issue (not a blocker)
`led_indicate_error()` uses raw `digitalWrite(pin_led_rx, HIGH/LOW)` (shared
non-NeoPixel path); on the XIAO's active-LOW LEDs the error blink colours are
inverted. Left alone to avoid regressing other boards.

### Next: flash + verify
1. Double-tap reset → bootloader, then flash (command below).
2. Expect: BLE advertises "RNode XXXX", green LED on at end of setup().
3. Device is UNPROVISIONED → KISS won't fully respond until provisioned with
   rnodeconf (product/model/serial/checksum + firmware hash).

## Build & flash workflow

Build: `cd "/home/idan/Downloads/Rnode_Firmware/Seeed Studio/XIAO nRF52840 Wio-SX1262" && ./build_xiao_adafruit.sh`

Flash (from bootloader mode — double-tap reset):
```bash
adafruit-nrfutil dfu serial --package /tmp/RNode_Firmware_XIAO/build/adafruit.nrf52.xiaoseeed/RNode_Firmware_XIAO.ino.zip -p /dev/ttyACM0 -b 115200 --singlebank
```

Device re-enumerates as `/dev/ttyACM0` (or `/dev/ttyACM1` etc).

## Key technical context

### InternalFS / EEPROM architecture
- Adafruit nRF52 core's InternalFS uses flash at 0xED000 (7 pages, 28KB, LFS_BLOCK_SIZE=128)
- Previous NVMC hack (Opus 4.8 session) wrote directly to 0xEC000, corrupting LittleFS metadata → caused `block_count` assertion crash
- `InternalFS.format()` clears all corruption but also wipes bonding data and EEPROM file
- `InternalFS.begin()` MUST be called after `Bluefruit.begin()` on XIAO because the SoftDevice's flash event handler isn't ready before BLE init
- `eeprom_begin()` is deferred to after `bt_init()` for this reason

### Boot sequence (current code in RNode_Firmware.ino.cpp)
```
setup()
  → LED init (active-LOW)
  → InternalFS.begin()
  → if !exists("/xiao_ok"): InternalFS.format()
  → eeprom_begin()
  → if needs_format: file.open("/xiao_ok"), file.close()  ← PROBLEM: reuses global `file` from EEPROM
  → bt_init() → Bluefruit.begin()
  → rest of setup...
```

### Known working state
- Flash with unconditional `InternalFS.format()` + `eeprom_begin()` before `bt_init()` → firmware boots, BLE advertises as "RNode AA6B", 10-blink red pattern (not_ready = unprovisioned)
- KISS serial does not respond (expected: unprovisioned device has `hw_ready=false`)
- Device re-enumerates on USB after flash

### Known failing state
- Flash with flagfile logic (`InternalFS.exists("/xiao_ok")` check) → device crashes silently, no LEDs, no USB-CDC, requires double-tap reset to recover

### XIAO-specific differences from T1000-E (must preserve)
- `BOARD_XIAO_NRF52=0x53` in Boards.h, `PRODUCT_XIAO_NRF52=0x21`, `MODEL_C0=0xC0`
- LED active-LOW (HIGH=off, LOW=on)
- LoRa hardware reset before preInit (Wio-SX1262 needs it)
- `while(!Serial)` exclusion (USB-CDC)
- `#include <Adafruit_TinyUSB.h>` (Adafruit core)
- SX1262 pin config and RF switch GPIO

### EEPROM functions (Utilities.h) — all use InternalFS file I/O
- `eeprom_begin()`: opens/creates EEPROM_FILE with XIAO branch that does format-on-failure
- `eeprom_read()`: file.seek + file.read
- `eeprom_flush()`: file.close + file.reopen
- `eeprom_update()`: file.seek + file.write
- `eeprom_erase()`: InternalFS.format()
- Global `File file(InternalFS)` used throughout

## Device info
- Seeed XIAO nRF52840 Sense + Wio-SX1262 (SKU 102010710)
- nRF52840 + Semtech SX1262, 902-928 MHz, 22 dBm
- USB VID:PID: 2886:0045
- Serial: B7EF3F9E69A92D94
- S140 7.3.0 SoftDevice, v7 ldscript (start 0x27000)

## Adafruit core vs Seeeduino core
Adafruit core (`adafruit:nrf52:xiaoseeed`) is used because:
- Seeeduino core's `InternalFS.begin()` → `sd_flash_write()` hangs permanently during `Bluefruit bond_init`
- Adafruit core uses S140 7.3.0 which properly manages flash operations
- Meshtastic's validated Seeed XIAO nRF52840 variant also uses Adafruit core