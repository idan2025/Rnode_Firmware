---
name: usb-cdc-blocking-write-freeze
description: "T1000E blocking USB-CDC Serial.write() that froze the whole loop, and the bounded-write fix"
metadata: 
  node_type: memory
  type: project
  originSessionId: 8b99a2c2-1bf8-4a91-bb86-75b39e8adbd6
---

**Bug:** `serial_write()` (Utilities.h) wrote to the USB-CDC via a bare `Serial.write(byte)` whenever `bt_state != BT_STATE_CONNECTED`. On the nRF52 TinyUSB CDC that blocks until the host drains the TX FIFO, so a host that holds the port open but stops reading (stalled terminal, ModemManager probing the CDC on plug-in, an app that connected then hung) freezes the entire `loop()` — radio RX/TX, KISS, and the BLE data path all stall. Separate from the boot-hang in [[ble-stops-on-usb-while-serial]].

**Fix (RNode_Firmware_recovered/Utilities.h):** added `usb_serial_write()` (nRF52 only) — bounded wait: a healthy host still gets every byte (waits while the FIFO drains via `yield()`), but if `availableForWrite()` stays 0 past `USB_TX_STALL_TIMEOUT_MS` (100 ms) it drops the byte and keeps the loop alive. One-shot `usb_tx_stalled` latch makes the rest of a stalled burst drop instantly; clears when the host resumes draining or closes the port. `serial_write()` routes all `Serial.write` paths through it.

**Benign side effect:** binary ~12 KB SMALLER (214→202 KB) — consolidating the write behind one function stopped the compiler inlining the heavy `Serial.write` body into every `kiss_indicate_*`. Verified via `nm --print-size` symbol diff (all functions present, just deduplicated). Build is deterministic.

**Verification caveat (important for any future repro attempt):** you CANNOT easily stage a before/after *freeze* from a Linux host — `cdc_acm` buffers the device's output in the kernel even when userspace never `read()`s, so the device's FIFO rarely actually fills on the bench (~40 KB of output got absorbed, no stall). Also note `bt_stop` on nRF52 only flips the state flag — it does NOT stop SoftDevice advertising — so don't use advertising as a loop-liveness side channel. Verify instead via: `rnodeconf -i` (clean heavy EEPROM read through the path), survives-undrained-flood-without-wedging, and code-correctness (bounded wait can't spin forever).

Distributed in the same image as the boot-hang fix: `.bin` sha256 `7d40803efe62f7d7037804edcdead25e0a66e5f1376b35e1fc16afc8308f9cd1`. Distribute per [[firmware-distribution-push-workflow]]; build via [[t1000e-build-flash-recipe]].
