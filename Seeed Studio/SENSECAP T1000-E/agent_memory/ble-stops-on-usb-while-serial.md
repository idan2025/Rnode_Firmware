---
name: ble-stops-on-usb-while-serial
description: "Why T1000E BLE stops advertising when plugged into USB, and the one-line fix"
metadata: 
  node_type: memory
  type: project
  originSessionId: 8b99a2c2-1bf8-4a91-bb86-75b39e8adbd6
---

**Symptom:** On the Seeed T1000-E (nRF52840 + LR1110) RNode firmware, BLE advertising stops as soon as the device is plugged into ANY USB source (computer, media streamer, even a dumb charger) and only comes back after manually re-enabling Bluetooth.

**Root cause:** `setup()` in `RNode_Firmware.ino` has `while (!Serial);` guarded by an exclusion list of nRF52 boards (RAK4631, Heltec T114, T-Echo, T3S3, TBEAM_S_V1, Heltec32_V4). **BOARD_T1000E was missing from that list**, so it executed the wait. `Serial` is the TinyUSB USB-CDC; the wait blocks `setup()` until a host opens the CDC port (asserts DTR). When the device reboots while powered from a USB source that never opens the port, `setup()` hangs *before* `bt_init()`, so BLE never starts. (USB attach induces a reboot on this hardware; on battery the path isn't hit, so it "works on battery.")

**Fix:** add `&& BOARD_MODEL != BOARD_T1000E` to the `#if BOARD_MODEL != ...` condition wrapping `while (!Serial);` (~line 200). Canonical source edited: `Seeed Studio/SENSECAP T1000-E/RNode_Firmware_recovered/RNode_Firmware.ino.cpp`.

**How it was proven (no physical replug needed):** send `CMD_RESET` (`C0 55 F8 C0`) then immediately close the port (drops DTR) → mimics a charger; BLE never returns. Open the port (asserts DTR) → firmware unblocks, BLE "RNode AA6B" advertises. After the fix, BLE advertises even with the port left closed. Confirm radio works on USB independently: `CMD_BT_CTRL 0x01` (`C0 46 01 C0`) starts advertising fine while on USB.

**Secondary bug, since FIXED (see [[usb-cdc-blocking-write-freeze]]):** `serial_write()` (Utilities.h) called blocking `Serial.write()` whenever `bt_state != BT_STATE_CONNECTED`; if the CDC is open but undrained the whole `loop()` can freeze. Advertising survives this (SoftDevice is independent), so it never explained the user symptom — but it was hardened anyway via a bounded `usb_serial_write()`.

Build/flash/verify recipe lives in [[t1000e-build-flash-recipe]].
