# RNode Firmware for Seeed SenseCAP T1000-E — Project State

Last updated: 2026-06-21. This file reflects the CURRENT state.
Everything below the "Resolved/historical" section at the bottom was
superseded — board-model detection, EEPROM validity, BLE reconnect, and
the basic build pipeline are all working now.

## RADIO RE-VERIFIED on the shipped firmware (2026-06-21, after LATEST-3+4 fixes) ✅
Confirmed TX and RX both still work on the distributed build (`.bin 7d40803e`, both fixes) — neither fix touches `lr1110.cpp`, and this proves the radio subsystem is unaffected.
- **Arm:** `radio_online=1` after KISS bring-up (917.8 MHz / 250 kHz / SF10 / CR5 / 20 dBm) → LR1110 SPI + version read + calibration all OK.
- **TX:** sent 3 LoRa packets; airtime (`CMD_STAT_CHTM` ats field) rose 0→381, **no TXFAILED** → PA keys up and transmits.
- **RX front-end:** noise floor steady **−112 dBm**, live `current_rssi` (settled to floor) → receiver digitizing RF (expected ~−110…−128 dBm).
- **RX over the air:** user TX'd from the Heltec; T1000E received **18 valid-CRC packets** (only good-CRC frames are forwarded as `CMD_DATA`), 9×167 B + 9×207 B, RSSI −58…−60 dBm, SNR 12–15 dB → full receive path demodulates real packets.
- **How to redo:** radio test scripts kept at `/tmp/radio_test.py` (arm+TX+noise), `/tmp/nf_mon.py` (noise-floor monitor), `/tmp/rx_watch.py` (logs received `CMD_DATA` packets w/ RSSI/SNR to `/tmp/rx_watch.log`). Production fw has NO `stat_rx`/`stat_tx` debug counters — use `CMD_DATA` capture + `CMD_STAT_CHTM` (0x25; noise_floor = byte9 − 157) instead of `rx_diag.py`'s counters. The Heltec is the user's TX radio (not passed through to the VM), so OTA RX needs the user to transmit.

## LATEST-4 (2026-06-21) — blocking USB-CDC write that froze the whole loop — HARDENED ✅
- **Bug:** `serial_write()` (Utilities.h) wrote to the USB-CDC via a bare `Serial.write(byte)` whenever `bt_state != BT_STATE_CONNECTED`. On the nRF52 that call **blocks until the host drains** the CDC TX FIFO; a host that holds the port open but stops reading (a stalled terminal, ModemManager probing on plug-in, an app that connected then hung) freezes the entire `loop()` — radio RX/TX, KISS and the BLE data path all stall until the host drains (documented in the "naive serial test scripts" lesson below). Independent of the LATEST-3 boot hang.
- **Fix:** new `usb_serial_write()` helper (nRF52 only) wraps the write with a bounded wait — a healthy host still gets every byte (it waits while the FIFO drains), but if the FIFO stays full past `USB_TX_STALL_TIMEOUT_MS` (100 ms) it drops the byte and keeps the loop alive. A one-shot `usb_tx_stalled` latch makes the rest of a stalled burst drop instantly instead of paying the timeout per byte; it clears the moment the host resumes draining or closes the port. `serial_write()` routes all its `Serial.write` paths through it. File: `RNode_Firmware_recovered/Utilities.h`.
- **Side effect (benign):** binary got ~12 KB SMALLER (214→202 KB). Consolidating the write behind one function stopped the compiler inlining the heavy `Serial.write` body into every `kiss_indicate_*` caller — confirmed via `nm` symbol-size diff (all functions present, just deduplicated). Deterministic build.
- **Distributed build (both LATEST-3 + this fix):** `.bin` 202280 B sha256 `7d40803efe62f7d7037804edcdead25e0a66e5f1376b35e1fc16afc8308f9cd1`; zip sha256 `89889f16405cc3f6e981fb89d6e8786c24d6aa9df3e2bec29d86164755bb8a1e`. Rolled into both distributed zips + the Release asset; flashed + verified on hardware (hash gate MATCH, `rnodeconf -i` clean — i.e. the heavy EEPROM-read path runs fine through the new helper under real draining — BLE intact, survives an undrained-flood without wedging).
- **Verification caveat:** a clean before/after *freeze* repro is hard to stage from a Linux host because `cdc_acm` buffers the device's output in the kernel even when userspace isn't `read()`-ing, so the device's FIFO rarely actually fills on the bench. Fix is established by code-correctness (bounded wait can't spin forever) + no-regression + survival, not a bench freeze/recover demo.

## LATEST-3 (2026-06-21) — BLE stops advertising as soon as USB is plugged in — FIXED ✅
- **Symptom reported:** unit works on battery, but the moment it's plugged into ANY USB source — a computer, a USB media streamer, or even a dumb charger — BLE stops advertising and stays off until Bluetooth is manually re-enabled (and it doesn't survive the next USB plug).
- **Root cause — `while (!Serial);` in `setup()` (RNode_Firmware.ino ~line 200).** `Serial` is the nRF52840 TinyUSB **USB-CDC** port; that wait blocks boot until a host actually opens the port (asserts DTR). The guard around it already excludes every other nRF52 RNode board (RAK4631, Heltec T114, T-Echo, T3S3, TBEAM_S_V1, Heltec32_V4) for exactly this reason — **`BOARD_T1000E` was simply missing from the list.** A USB attach induces a reboot; if the power source never opens the CDC port (charger / streamer / host before RNS connects), `setup()` hangs **before `bt_init()`**, so BLE never starts. On battery the path isn't hit, so it "works on battery."
- **Fix:** add `&& BOARD_MODEL != BOARD_T1000E` to the `#if BOARD_MODEL != ...` guard wrapping `while (!Serial);`. One line. File: `RNode_Firmware_recovered/RNode_Firmware.ino.cpp:200`.
- **Proven on hardware without a physical replug:** send `CMD_RESET` (`C0 55 F8 C0`) then immediately close the port (drops DTR) = mimics a charger → BLE never returns; open the port (asserts DTR) → BLE `RNode AA6B` advertises. After the fix, BLE advertises even with the port left closed. Isolate that the radio works on USB independently: `CMD_BT_CTRL 0x01` (`C0 46 01 C0`) starts advertising fine while on USB.
- **Built + distributed:** `.bin` 214568 B; zip sha256 `04d9e985f154bb0f7b0c265088eefee263dcda7f6400ed6e4cc0c7017520d2d7` (inner `.bin` sha256 `4f907709ec86afb962b994f61363a7ad808a74291ff9050a5aacc2aefff3a0f6`). Rolled into BOTH `firmware/rnode_firmware_t1000e.zip` (web flasher) and `Seeed Studio/.../rnode_firmware_seeed_t1000e_lr1110.zip` (production), and the GitHub Release `v1.0-t1000e` asset (clobbered). Committed + pushed to `main` (`bd07362`).
- **Web flasher needs NO change** and was verified end-to-end: its T1000-E `firmware_url` is the raw `main` zip (no pinned hash), served with `access-control-allow-origin: *`; the served `.bin` is bit-identical to the committed-source rebuild; and its "Set Firmware Hash" step reads the device's live hash, so it self-adapts to the new image. Just push the zip to `main` (CDN ~5 min) and it's live.
- **Secondary latent bug (NOT the cause of this symptom; fixed separately in LATEST-4 above):** `serial_write()` used a blocking `Serial.write()` whenever `bt_state != BT_STATE_CONNECTED`; if the CDC is open but undrained the whole `loop()` can freeze. BLE advertising survives it (SoftDevice is independent), so it never explained the advertising-stop symptom — but it was worth hardening anyway.

## LATEST (2026-06-20, this session) — `dcd()` carrier-detect bug fixed; cache rebuilt; provisioning gotcha
- **Symptom reported:** direct RNode↔RNode over LoRa (no bridge): a 380-char message never arrived (RX LED flashes then off, never delivered); one-word messages very slow. TX/announces worked.
- **Root cause — `lr1110::dcd()` (channel/carrier detection).** The LR1110 port rewrote `dcd()` with *sticky* `sticky_header`/`sticky_preamble` latches but, unlike the `sx126x::dcd()` it was modeled on, **never cleared the chip's latched `PREAMBLE_DETECTED`/`HEADER_VALID` IRQ bits** (on the LR11xx those status bits stay latched until explicitly cleared). So once the radio saw a single header, `dcd()` returned `carrier_detected=true` *forever* (until the next clean RX_DONE). In `update_modem_status()` (RNode_Firmware.ino.cpp:1469-1502) that stuck signal (a) pins the RX LED on, and (b) makes CSMA `medium_free()` see the channel as permanently busy → the node almost never gets a clear TX slot → link acks/proofs throttled (slow one-word msgs) and multi-packet Resource transfers (the 380-char msg) never complete.
- **Fix:** rewrote `lr1110::dcd()` to mirror the proven `sx126x::dcd()` — read header/preamble **live** from the IRQ status each poll (no sticky caching), and clear **only** the `PREAMBLE_DETECTED` bit on false-preamble window expiry (a single-bit clear can't disturb a pending RX_DONE). Removed the dead `sticky_header`/`sticky_preamble` statics and their assignments in `handleDio0Rise()`. File: `RNode_Firmware_recovered/lr1110.cpp`.
- **Rebuilt + redeployed:** new clean build 214384 bytes → `rnode_firmware_seeed_t1000e_lr1110.zip` sha256 `f77a22e22e5ffb7573db2ebb9ba07002c782f794d6ab82ecd6e50b9da5e03e03` (prev image saved `…_predcdfix.bak.zip`). rnodeconf cache fully synced to the new hash everywhere: all 3 `rnode_firmware_t1000e.zip`+`.bin` (firmware/, update/1.86/, update/latest/), both `.version` files, AND `release_info.json`, `release_info.json.local`, `fallback_release_info.json(.local)`, `version_release_info.json`. Verified no stale `dc6c7235…` hash remains. **NOTE: there are more release_info variants than the old AGENTS list — also update `fallback_release_info*` and `version_release_info.json` or rnodeconf may verify against a stale hash.**
- **Flashed to the unit via direct nrfutil** (Method B). Hash gate then needs re-sync (direct flash breaks it) — but see provisioning note below.
- **PROVISIONING GOTCHA (important):** `rnodeconf -u -U` is **update-only and refuses an unprovisioned device** — on a wiped EEPROM it stops at "Could not download EEPROM" / "Device not provisioned. Cannot update device firmware." (rnodeconf.py:3669-3670), exits non-zero, and does **neither provision nor hash-sync** (the auto-hash-sync at rnodeconf.py:3811-3814 is gated behind `if rnode.provisioned`). To provision a blank/wiped unit use the **bootstrap** path: `rnodeconf -r --product 1e --model b5 --hwrev 1 <port>` (patched flow: writes EEPROM + enables BLE), then `python hash_sync.py <port> --write`; or `rnodeconf --autoinstall <port>` for a from-scratch unit (does flash+EEPROM+hash+BLE in one go, autoinstall hash-sync at rnodeconf.py:4385-4400).
- **`provision_t1000e.sh` was buggy for the "firmware present + EEPROM wiped" case** (step 1 saw firmware via `-i` exit 0, ran `-u`, which exits 1 on blank EEPROM → aborted before provisioning). **FIXED**: step 1 now only runs `-u` when the device is *also* provisioned (`-i` shows "Device signature   : Validated"); a firmware-present-but-unprovisioned unit skips `-u` and falls straight through to the `-r` provisioning in step 2.
- USB re-enumeration is real and frequent: the unit hops between `/dev/ttyACM0` and `/dev/ttyACM1` after every flash/reset (it was at ACM0→ACM1→ACM0 this session). Always re-resolve the port (e.g. via `/dev/serial/by-id/usb-Seeed_Tracker_T10000_E_LoRaWAN_*`) after any flash/reset. `hash_sync.py`'s timing is fragile right after boot; the device answers reliably only when drained continuously for a second or two first.

## LATEST-2 (2026-06-20) — LARGE-MESSAGE RX root cause FOUND via live test & FIXED ✅
- **Symptom:** short LXMF messages delivered fine, but a 380-char message (a 528-544 B LXMF **Resource in 2 parts**) never arrived; RX "super flaky even after a few short messages." Reproduced over BOTH USB-serial and BLE/Columba at SNR 14 -> radio-side, not link/BLE.
- **How it was found:** built a live LXMF endpoint on this PC over the T1000E (`lxmf_live.py`, announces an address Columba can message), with a probe wrapping `RNodeInterface.process_incoming/outgoing` to log EVERY raw packet len, plus temporary firmware counters. **Key observation: no raw RX packet was ever >211 B, and 211 B == our own announce/TX size.**
- **DOMINANT ROOT CAUSE — explicit-header RX payload-length cap.** On the LR1110, `pld_len` in the LoRa packet params is the **maximum accepted RX length** in explicit-header mode (a longer packet is silently dropped, never raises RX_DONE) - UNLIKE the sx126x, where explicit-header RX ignores `pld_len`. `lr1110::receive(0)` re-armed RX via `explicitHeaderMode()` using `_payloadLength`, which `endPacket()` leaves at the **last TX size** (announces ~211 B). So RX silently rejected anything bigger than the last packet we transmitted -> the full ~255 B first half of a split 380-char message never arrived, while short messages did; messages near the cap were flaky. **Fix:** in `lr1110::receive()` explicit branch, `_payloadLength = MAX_PKT_LENGTH (255)` before `explicitHeaderMode()`. (`RNode_Firmware_recovered/lr1110.cpp`.)
- **Secondary fix (also shipped): 256-byte RX buffer wrap.** `lr11xx_regmem_read_buffer8` reads a linear span and doesn't wrap at the 256-byte boundary; in RX-continuous the write pointer advances, so a large packet with `start+len>256` had its tail read as garbage. New `loadPacket()` splits the read at the boundary. Plus the earlier **dcd sticky-carrier/CSMA** fix. All three are in production.
- **VERIFIED via the same live endpoint after the fix:** raw RX now shows 233 B and **483 B reassembled split frames**; **four 373-char messages delivered**; LXMF resource transfers "Transfer concluded" in ~5 s (occasional single "timed out waiting for 1 part" retry then concludes = normal half-duplex LoRa, NOT a firmware bug; the old failure was 15+ retries that never concluded).
- **Production image:** `rnode_firmware_seeed_t1000e_lr1110.zip` sha256 `8972216d5556f76150029107d878f6da0c179fa6e03e77025f536a5bb0c2b08b`, 214480 B, **diagnostics removed** (temporary `stat_rxbig`/`stat_lastrxlen`/`CMD_STAT_DBG 0x63` instrumentation used to localise this was reverted). Flashed, provisioned (1e/b5/1, signature Validated), hash MATCH, BLE enabled. Cache fully synced to this hash. Debug build with the counters kept as `rnode_firmware_seeed_t1000e_lr1110_DEBUG_splitdiag.zip`; reader is `read_diag.py`.
- **Live-test tooling kept:** `lxmf_live.py` (LXMF endpoint + RX/TX probe; needs `lxmf` pip pkg, now installed in the venv; set `RNS.loglevel=LOG_EXTREME` AFTER `RNS.Reticulum()` or config resets it). Lesson: a 4-char LXMF message is ~211 B on-wire; messages big enough to become a Resource (2 parts) are what exercise the split/large-packet path - announces (~198 B) are single packets and never did.

## Device info
- Device: Seeed SenseCAP T1000-E (nRF52840 + Semtech LR1110)
- Serial port: `/dev/ttyACM0` (NOT ttyACM1 — that was an earlier session's enumeration)
- Constants: PLATFORM_NRF52=0x70, MCU_NRF52=0x71, BOARD_T1000E=0x52, PRODUCT_T1000E=0x1E, MODEL_B5=0xB5
- Firmware source (ORIGINAL): `/tmp/RNode_Firmware` — **WIPED by the 2026-06-20 reboot (/tmp is not durable).**
- Firmware source (RECOVERED, durable): `/home/idan/Downloads/test/RNode_Firmware_recovered/` — recovered from the Arduino build cache (`~/.cache/arduino/sketches/984502505D161CCD1B3D578A93422299/sketch/`), which survived the reboot and contains the full custom `lr1110.cpp`/`lr1110.h` + lr11xx SDK + all upstream RNode files. The `.ino` is present as the preprocessed `RNode_Firmware.ino.cpp` (has `#line` markers). If a clean rebuild is needed, re-clone markqvist/RNode_Firmware and drop the custom files back in.
- `~/.reticulum/config` has a real, pre-existing `[[AA6B]]` RNodeInterface block: 917.8MHz / 250kHz BW / SF10 / CR5 / 20dBm txpower / port=/dev/ttyACM0. Don't change txpower without reverting it back to 20 when done testing.
- Diagnostic/test scripts (all in `/home/idan/Downloads/test/`, per user's "keep all files in test folder"): `hash_sync.py` (read/compare/sync the fw-hash gate), `rx_diag.py` (KISS bring-up + counter monitor), `rx_monitor.py` (passive RX monitor). venv: `/home/idan/Downloads/venvs/rns/bin/python`.

## PRODUCTION BUILD + TURNKEY PROVISIONING (2026-06-20, final) ✅
- **Clean production firmware:** `rnode_firmware_seeed_t1000e_lr1110.zip` — diagnostic `stat_rx`/`stat_tx` ISR counters REMOVED from `lr1110.cpp handleDio0Rise()` (continuous-RX fix retained). 214432 bytes. Built via `arduino_build/`. Installed into the rnodeconf cache (firmware/ + update/latest/ + update/1.86/ as `rnode_firmware_t1000e.zip` + `.bin`; `.zip.version`/`.version.latest` updated to `1.86 <sha256-of-clean-zip>`; t1000e entry added to release_info*.json). sha256 of the clean zip at build time: `dc6c72352d92ce321c5241581e7451386ad6e773e79bf63005240a12778060fc` (changes each rebuild).
- **Debug firmware kept:** `rnode_firmware_seeed_t1000e_lr1110_debug.zip` (the build WITH the stat_rx/stat_tx counters, readable over CMD_STAT_RX/TX).
- **One-command provisioning:** `provision_t1000e.sh [serial-substring]` — flashes clean fw (`rnodeconf -u`, or `--autoinstall` if blank) → provisions EEPROM (1e/b5/1) → BLE enable + hash sync (patched rnodeconf) → verifies hash MATCH. **Robust to USB re-enumeration**: a `wait_port()` helper polls by VID:PID 2886:8057 and settles after every flash/reset. Must export the venv `bin` onto PATH (rnodeconf shells out to `adafruit-nrfutil` by name). Verified end-to-end on the test unit: flash→reenum→provision→reenum→hash-sync→reenum→MATCH→`radio_online=1`.
- `rnodeconf -u -U --nocheck --fw-version 1.86 <port>` confirmed: verifies integrity against the new `.version` hash, flashes the clean build, reconnects across the flash re-enumeration, and auto-syncs the hash. Counters now report 0 (instrumentation gone) — that's expected/correct.

## BIDIRECTIONAL CONFIRMED (2026-06-20, final) — RX + TX both work; firmware is sound ✅
- **RX (network → T1000E): CONFIRMED & sustained.** With a stable bridge (phone hotspot) and `node_soak.py` (now ping-enabled), the TCP node hears the T1000E's announces and sends addressed ping packets back; the **T1000E received 4 pings, one every ~10s, perfectly steady** (`*** PING RECEIVED ... ping from TCP #N` in `soak_lora.log`). The firmware receives and delivers addressed network traffic reliably.
- **TX (T1000E → network): CONFIRMED & sustained** (TCP node heard 15 announces over 219s earlier; steady here too).
- **The earlier "RX asymmetry"/`data_pkts=0` was NOT a firmware bug** — it was the **bridge's announce-forwarding policy**: it relays *addressed data* both ways but only re-broadcasts *announces* LoRa→TCP (not TCP→LoRa), so the T1000E never "HEARD" the TCP node's announces yet received its pings fine. Don't chase this as a firmware RX bug.
- **RNS RNodeInterface patch (venv-local):** patched `validateRadioState()` in `RNS/Interfaces/RNodeInterface.py` — the serial path waited only 0.25s before validating radio state, but the T1000E's `begin()` blocks ~3s while arming (can't answer serial during it), causing a false "Radio state mismatch" → abort/reconnect → degraded RX. Patch polls/re-asks radio state for up to ~6s. After it, the RNodeInterface comes up clean ("configured and powered up", no mismatch). Lost if the RNS venv is reinstalled.

## Bidirectional test status (2026-06-20, earlier) — TX CONFIRMED; soak limited by intermittent bridge
- **TX from T1000E → network: ROBUSTLY CONFIRMED.** With `node_soak.py lora` (RNS over the T1000E RNodeInterface) announcing and `node_soak.py tcp` listening, the TCP node heard **15 of the T1000E's announces over a 219-second span** (`heard_from_peer=15`, seqs 1..23). T1000E transmits valid Reticulum announces that propagate LoRa→bridge→TCP. ✅
- **RX radio level: confirmed** — `stat_rx` reached 104, zero CRC errors (`stat_tx=0`) across the session.
- **RX delivery to host: confirmed earlier** (first clean burst: 5 announces delivered as CMD_DATA with EXACT matching destination hashes).
- **Bridge relay is INTERMITTENT** (user's router "in a bad position"): it relays in bursts (e.g. 14:06–14:11 stat_rx 0→104) then goes silent for many minutes. This blocked a clean *sustained simultaneous bidirectional soak* and made RX-delivery re-confirmation flaky (fresh-announce hash-match test only lands during a burst). The intermittency is bridge/router-side — the T1000E radio stays `online=1`, re-arms cleanly, and `stat_rx` climbs whenever the bridge transmits.
- **One open observation:** during the bidirectional window the RNS lora node logged 0 SOAK announces from the TCP peer while the TCP node heard 15 from it — most likely the bridge's TCP→LoRa direction wasn't relaying those announces during that window (asymmetric/intermittent relay + announce routing), since raw-KISS delivery (data=5, matching hashes) proves the firmware DOES deliver received packets. Re-test when the bridge/router is stable.

## (earlier) Soak / bidirectional test status (2026-06-20) — BLOCKED BY BRIDGE, not the T1000E
- RX fix re-confirmed; radio proven healthy (cleanly cycles `CMD_RADIO_STATE` 0→1, stays `online=1`, instant serial response).
- Tried a full bidirectional soak: two independent RNS instances — `node_soak.py lora` (RNS over the T1000E RNodeInterface; RNS logs "RNodeInterface[T1000E] is configured and powered up" and it transmits announces) and `node_soak.py tcp` (RNS over TCP to 192.168.223.20:4242). Each announces fresh destinations; handler logs peer announces.
- **Result: neither heard the other (`heard_from_peer=0` both ways) and the T1000E's `stat_rx` froze at 44.** The bridge relayed TCP→LoRa fine from ~13:35–13:38 (that's how RX was proven — `stat_rx` 0→44, real announces delivered with matching hashes), then **went silent ~13:38 and stayed silent**. My TCP side is healthy (server reachable, `netann` climbing = announces seen on the TCP network), so the gap is the **bridge no longer relaying over LoRa** (bridge-side / environmental — Pi Zero+Xiao+Wio SX1262). LoRa→TCP (TX-to-network) was never confirmed either.
- **To finish the soak + TX-to-network + bidirectional: need the bridge actively relaying again** (restart/verify it), or a manual Heltec spam to re-confirm sustained RX. Scripts ready: `node_soak.py` (bidirectional), `bridge_test.py` (raw-KISS RX via bridge). The T1000E firmware/radio is not the blocker.

## RX IS FIXED AND VERIFIED END-TO-END (2026-06-20) ✅
The continuous-RX fix was built, flashed, hash-synced, and **verified working over the LoRa<->TCP bridge**: 8 announces injected from this host over TCP to the Reticulum server `192.168.223.20:4242` were relayed by the bridge (Xiao ESP32-S3 + Wio SX1262) over LoRa and **received by the T1000E** — `stat_rx=5, stat_tx(CRCerr)=0, data_pkts=5`, every delivered `CMD_DATA` payload (198 B, reassembled 2-packet split announce) carrying the exact destination hash that was announced (`651220a1…`). `stat_rx` climbs steadily (was frozen at 1 before the fix), proving continuous RX + split reassembly + host delivery all work. Final working firmware: `test/rnode_firmware_t1000e_lr1110_continuousrx.zip` (and the same zip in the build dir). See `Result.md`.
Remaining optional polish: the temporary `stat_rx`/`stat_tx` diagnostic counters are still in `handleDio0Rise()` (harmless — repurpose dead fields — and useful for future RX debugging); remove them if a production-clean build is wanted (needs another build+flash+hash-sync cycle). Build toolchain is now durable at `test/arduino_build/` (arduino-cli + Seeeduino:nrf52@1.1.13 + Crypto lib); FQBN `Seeeduino:nrf52:tracker_t1000_e_lorawan`, compile needs `adafruit-nrfutil` on PATH (venv) and `-DBOARD_MODEL=0x52`.

## Current status (2026-06-20, this session) — MAJOR: dead-radio root cause found & fixed
- **The radio was DEAD (not RX-specifically-broken): root cause = firmware-hash gate mismatch.**
  - `device_init()` (Device.h:218) returns false unless `bt_ready` AND `fw_signature_validated` (live SHA256 of the app flash == target hash stored in EEPROM). False → `hw_ready=false` → `startRadio()` silently refuses (warning LED only, NO CMD_ERROR) → radio never arms → no RX *and* no TX, while serial/BLE/EEPROM all keep working and mask it.
  - Confirmed via `hash_sync.py`: stored target `ffa6d5…` ≠ live `705a37…`. The prior session's **direct adafruit-nrfutil flash of the diagnostic build did not sync the hash**, leaving the gate mismatched. So the previous "RX broken, stat_rx=0" conclusion was **confounded — the radio was simply off the whole time.**
  - **FIXED this session**: wrote the live hash back as the target via `CMD_FW_HASH` (0x58) → device saved + hard-reset → re-verified `705a37…` == `705a37…` (MATCH).
- **Radio now arms**: after the fix, a KISS bring-up (config 917.8/250/SF10/CR5/20dBm → `CMD_RADIO_STATE=1`) returns `CMD_RADIO_STATE=0x01` (radio_online=1) and the device emits unsolicited `CMD_STAT_CHTM`(0x25)/`CMD_STAT_PHYPRM`(0x26) telemetry = radio genuinely online. Device mode is "Normal (host-controlled)" = MODE_HOST, so it does NOT auto-arm at boot; a host (rnsd, or our KISS bring-up) must configure + turn it on.
- **RX end-to-end FIRST honest test DONE — real RX bug found (continuous-mode):** with the radio armed (online=1) and the Heltec spamming announces, `stat_rx` went 0→**1** with `stat_tx`(CRC errors)=0, then **froze at 1** across repeated windows; `data_pkts`(CMD_DATA to host)=**0**. So the radio DOES receive a clean packet (RF/freq/sync-word/modulation/IRQ wiring all work — validates the pulldown + dcd-race fixes), but only ONE, then stops.
- **ROOT CAUSE (datasheet-confirmed): single vs continuous RX mode.** `lr1110.cpp receive()` called `lr11xx_radio_set_rx(CTX, 0)` labeled "continuous" — but per `lr11xx_radio.h:307-309`, timeout `0x000000` = **RX SINGLE** (one packet → standby), only `0xFFFFFF` = **RX CONTINUOUS**. RNode's main loop never re-arms RX after a received packet (only at startup + after TX) and relies on continuous mode — exactly like `sx126x.cpp receive()` which issues `OP_RX` with `{0xFF,0xFF,0xFF}`. In single mode the LR1110 received the FIRST half of a (2-packet split) announce, buffered it without delivering, then dropped to standby — never getting the second half or any further packet. **One root cause explains both `stat_rx`-frozen-at-1 AND `data_pkts=0`.**
- **FIX applied to recovered source** (`RNode_Firmware_recovered/lr1110.cpp receive()`): replaced `lr11xx_radio_set_rx(CTX, 0)` with `lr11xx_radio_set_rx_with_timeout_in_rtc_step(CTX, 0xFFFFFF)` (raw rtc_step continuous sentinel; the ms→rtc_step conversion in `set_rx` would not yield exactly 0xFFFFFF). **NOT yet built/flashed** — toolchain was wiped with /tmp; rebuilding (see below). After flashing, re-sync the hash (`hash_sync.py --write`) and re-test RX.

## Real bugs found and fixed this project (in lr1110.cpp unless noted)

1. **RX IRQ pin floating** — `pinMode(_dio0, INPUT_PULLDOWN)` required (was plain `INPUT`). Confirmed against Seeed's reference `T1000EHardware.hpp` (`NRF_GPIO_PIN_PULLDOWN`). Without it the RISING-edge interrupt for RX_DONE doesn't reliably fire.

2. **dcd()/RX_DONE race** — `dcd()` is polled every 3ms from the main loop to drive LEDs and originally used the destructive `lr11xx_system_get_and_clear_irq_status()`. This raced with the real RX_DONE interrupt handler reading the same shared register — if the poll won, it silently erased RX_DONE before the ISR saw it (LED still blinks, payload dropped). Fixed: `dcd()` now uses the non-destructive `lr11xx_system_get_irq_status()` (peek-only), matching how `sx126x.cpp` already does it.

3. **On-device firmware-hash self-validation gate** (`Device.h`/`VALIDATE_FIRMWARE`) — after ANY manual reflash (not through rnodeconf's own flash step), the device computes a live SHA256 over its own flash and compares to a target hash stored in EEPROM. Mismatch → `hw_ready=false` → radio silently never starts, while BLE/serial still respond normally (totally masks the problem). Fix: after every reflash, query the device's own self-computed hash (CMD_HASHES/0x60 subtype 0x02) and write it back as the target (CMD_FW_HASH/0x58). This is now automated inside `rnodeconf.py`'s `-u`/update flow (see below) — but **NOT** automated when flashing directly via `adafruit-nrfutil` (which is what I've been doing this session for speed — see Build/Flash section). Direct nrfutil flashes might leave the hash gate mismatched; if the radio mysteriously stops initializing after a direct nrfutil flash, re-sync the hash via rnodeconf or manually.

4. **PA (power amp) lookup table** — `setTxPower()` originally hardcoded one `pa_duty_cycle`/`pa_hp_sel` pair for the whole LP/HP range. The LR1110's correct bias point varies per dBm target. Ported Seeed's exact `LR11XX_PA_LP_LF_CFG_TABLE`/`LR11XX_PA_HP_LF_CFG_TABLE` from `ral_lr11xx_bsp.c` as lookup arrays, verified byte-for-byte via a Python parse-and-compare script against the source header (don't hand-transcribe these tables — a first attempt had an off-by-one error).

5. **TCXO startup delay 6x too short** (found this session, the most likely RX/TX accuracy culprit): `enableTCXO()` was calling `lr11xx_system_set_tcxo_mode(CTX, LR11XX_SYSTEM_TCXO_CTRL_1_8V, 164)` — 164 ticks × 30.52µs/tick ≈ 5ms. Seeed's reference (`smtc_modem_hal_get_radio_tcxo_startup_delay_ms()` = 30ms, RTC freq 32768Hz) requires **983 ticks ≈ 30ms**. Fixed to `983`. A too-short TCXO settle time lets the PLL calibrate (and TX/RX proceed) against an unstabilized clock reference — mistunes the carrier silently, with NO local status check catching it (TX_DONE still fires, frequency readback still looks correct). This is a real, verified-against-reference fix, but empirically TX worked even before/after it and RX still doesn't work after it either — so it likely wasn't the (sole) root cause of the RX failure, even though it's a legitimate correctness fix worth keeping.

## Diagnostic instrumentation currently in the tree (TEMPORARY — remove once RX is fixed)

In `lr1110.cpp`'s `handleDio0Rise()`: repurposed the dead `stat_rx`/`stat_tx` globals (declared in `Config.h`, never incremented anywhere in upstream RNode_Firmware — confirmed via repo-wide grep) as:
- `stat_rx` = count of RX_DONE IRQ fires (any, including CRC errors)
- `stat_tx` = count of those that had a CRC error

These are readable over the **existing** KISS commands `CMD_STAT_RX` (0x21) and `CMD_STAT_TX` (0x22) without needing a new command — query raw via:
```python
import serial, time
s = serial.Serial('/dev/ttyACM0', baudrate=115200, timeout=2)
FEND=0xC0
s.write(bytes([FEND, 0x21, 0xFF, FEND]))  # CMD_STAT_RX query (send byte 0xFF as the "get" trigger byte the firmware expects — check RNode_Firmware.ino CMD_STAT_RX handler for exact request semantics before relying on this)
time.sleep(1)
print(s.read(200).hex())
```
**This was added but not yet exercised against a real test** — the next session should ask the user to spam announces again, then read these two counters. If both stay at 0 → RX_DONE never fires at all (point to IRQ/interrupt wiring, or the chip genuinely isn't receiving the RF). If `stat_rx` increments but `stat_tx` (CRC errors) equals it → packets are being detected but always fail CRC (point to modulation params, SF/BW/CR mismatch, or frequency error). If `stat_rx` increments with some CRC-clean → the radio layer is fine and the bug is upstream in KISS/RNS — much narrower problem.

## Key process lesson from this session: don't trust naive serial test scripts

Spent a long time chasing what looked like a fatal firmware hang (device responds once after boot/reflash, then goes completely silent on /dev/ttyACM0 — `serial.Serial().write()` would block forever). **This was NOT a firmware bug.** It was caused by my own test scripts reading the port too infrequently/in small bursts. The T1000E firmware sends periodic unsolicited status frames; if the host doesn't drain continuously, the TinyUSB CDC TX ring buffer fills, `Serial.write()` blocks synchronously inside the firmware's main loop, and EVERYTHING freezes (not just serial — the whole `loop()`) until the host drains enough. Confirmed by testing with a tight continuous-drain read loop, which stayed perfectly responsive for 17+ seconds straight. **Lesson: any raw serial probe script against this device must continuously drain in a tight loop, not poll with multi-second gaps between reads**, or it will look exactly like a fatal hang when it's actually self-inflicted backpressure.

This also means: don't conclude the firmware is broken just because a quick one-shot Python probe times out — retry with a continuously-draining read loop before assuming a real regression.

## Build & flash workflow that actually works (current, confirmed this session)

### One-command build (preferred) — `build_t1000e.sh`
`./build_t1000e.sh` (in this folder) does the whole thing: reconstructs the buildable sketch in `/tmp/RNode_Firmware` from the tracked (preprocessed) source, writes the arduino-cli config, and compiles — printing the output DFU zip path. Use this instead of redoing the manual steps below by hand. It exists because the buildable sketch lives in `/tmp` (wiped on reboot) and the tracked `RNode_Firmware_recovered/RNode_Firmware.ino.cpp` is the Arduino-PREPROCESSED form (has cosmetic `#line` directives; must be renamed to `.ino` and the directives stripped, or library auto-discovery / the preprocessor break). The script derives all paths from its own location and the bundled `arduino_build/` toolchain.
- `./build_t1000e.sh` — reconstruct from tracked source + build (always in sync with the repo).
- `./build_t1000e.sh --from-snapshot` — restore the saved ready-to-build sketch from `t1000e_ready_sketch.tar.gz` (a gitignored, on-disk literal copy of the last good `/tmp` sketch) instead of reconstructing.
- Output: `/tmp/RNode_Firmware/build/Seeeduino.nrf52.tracker_t1000_e_lorawan/RNode_Firmware.ino.zip`. Harmless warnings: `HAS_TCXO/HAS_INPUT/HAS_SLEEP redefined` (Boards.h defaults then per-board overrides).
- After building, flash with Method B below, then `python hash_sync.py <port> --write` to restore the on-device hash gate, then distribute per "Firmware distribution" (roll the zip into BOTH distributed copies + the Release asset; see the agent memory `firmware-distribution-push-workflow`).

The manual equivalent (what the script automates) is documented below for reference.

### Arduino CLI config
- Use `/tmp/arduino-config/arduino-cli.yaml` as `--config-file` (NOT `/tmp/RNode_Firmware/arduino-cli.yaml`, which only has board-manager URLs, no `directories:` section — using it gives "No platforms installed").
- That config points `directories.data` → `/tmp/arduino-config/data`, `directories.user` → `/tmp/arduino-sketchbook`.

### Compile command (confirmed working)
```bash
/tmp/bin/arduino-cli --config-file /tmp/arduino-config/arduino-cli.yaml compile \
  --fqbn Seeed_Studio:nrf52:tracker_t1000_e_lorawan -e \
  --build-property "compiler.cpp.extra_flags=\"-DBOARD_MODEL=0x52\"" \
  /tmp/RNode_Firmware
```
`BOARD_MODEL` is NOT defined anywhere by default for this board (only a `BOARD_GENERIC_NRF52` fallback exists in `Boards.h` if nothing is passed) — it MUST be supplied via this `-D` flag or compilation falls through to "unsupported nRF board" error. The project's own `Makefile` has this exact pattern for every other board (`-DBOARD_MODEL=0x..`) but never had a T1000E entry — consider adding one.

Output: `build/Seeed_Studio.nrf52.tracker_t1000_e_lorawan/RNode_Firmware.ino.zip` (a DFU package with `manifest.json` + `.bin` + `.dat` inside — same format rnodeconf's cache expects).

### Flashing — two working methods
**Method A (rnodeconf, when the app is already running and responsive):**
```bash
source ~/Downloads/venvs/rns/bin/activate
rnodeconf -u -U --nocheck --fw-version 1.86 /dev/ttyACM0
```
Note both `-u` (update) AND `-U` (force, to bypass the "already installed" version-string skip, since the version string stays "1.86" across rebuilds) are required — `-U` alone does nothing without `-u`. This path also auto re-syncs the on-device firmware-hash gate (point 3 above) — preferred when it works.

**Method B (direct nrfutil, faster/more reliable when the device is in a weird state or rnodeconf's KISS probe is being flaky):**
```bash
# Touch-reset into bootloader (1200 baud open+close):
python3 -c "import serial,time; s=serial.Serial('/dev/ttyACM0',1200); time.sleep(0.3); s.close()"
sleep 2
source ~/Downloads/venvs/rns/bin/activate
adafruit-nrfutil --verbose dfu serial --package <path/to/RNode_Firmware.ino.zip> -p /dev/ttyACM0 -b 115200 --singlebank
```
This leaves the device in app mode automatically at the end ("Activating new firmware"). Does NOT auto-sync the firmware-hash gate — if the radio doesn't come up after this, check/re-sync the hash manually via rnodeconf's `-i`/hash commands.

After flashing, the device needs ~1-3s to actually become responsive (radio init happens in `setup()`); the very first probe right after boot usually succeeds, then stays responsive if drained continuously (see lesson above).

### rnodeconf cache sync (REQUIRED after every rebuild, or `--autoinstall`/`-u` will flash a stale cached build)
Files to update (all under `~/.config/rnodeconf/`):
- `update/1.86/rnode_firmware_t1000e.zip` (+ `.bin`, `RNode_Firmware.ino.bin`, `RNode_Firmware.ino.dat`, `manifest.json`)
- `update/latest/rnode_firmware_t1000e.zip` (+ `.bin`)
- `firmware/rnode_firmware_t1000e.zip` (+ `.bin`)
- Hash strings in: `update/1.86/rnode_firmware_t1000e.zip.version`, `update/rnode_firmware_t1000e.zip.version.latest`, `update/release_info.json(.local)`, `update/fallback_release_info.json(.local)`

**Important subtlety**: the integrity-check hash rnodeconf verifies before flashing (`ensure_firmware_file`/"Verifying firmware integrity..." in rnodeconf.py) is the SHA256 of the **`.zip` file itself**, not the `.bin` inside it. Don't confuse this with the separate on-device self-validation hash (point 3 above), which is a different hash over a different thing (the running application's flash region). Conflating these two hashes wastes a flash cycle (got "Firmware corrupt" once this session from exactly this mistake).

```bash
sha256sum path/to/RNode_Firmware.ino.zip   # this is the hash that goes in release_info.json etc.
```

### rnsd / testing workflow
```bash
source ~/Downloads/venvs/rns/bin/activate
rnsd >/tmp/rnsd.log 2>&1 & disown
sleep 8
rnstatus   # check RNodeInterface[AA6B] Status: Up, sane Noise Fl. reading (~-110 to -127dBm)
```
- `rnsd` uses `share_instance` from `~/.reticulum/config` — only one instance needed; if you reflash the device while an old `rnsd` is holding the port open, that old instance's interface will go permanently "Down" (it doesn't recover from the device disappearing mid-DFU). Always `pkill -9 -f "bin/rnsd"` and restart `rnsd` fresh after any reflash.
- The interface occasionally shows `Down` on the very first `rnsd` start after a flash (same general flakiness as everything else touching this serial port in this sandbox) — kill and restart once if so; don't loop indefinitely.
- Listener script for real-world tests: `/tmp/rns_listen.py` (registers an `RNS.Transport` announce handler, runs for 60s).

## Sandbox/testing constraints learned this session (important for next time)
- **We're in a QEMU VM.** The T1000E passes through as `/dev/ttyACM0` (udev `ID_SERIAL_SHORT=6EAF1F35E2309DF5`, mode id `2886:8057`). The Heltec V3 is NOT passed through — it's the user's separate physical test radio.
- **`rnsd` will NOT stay alive across Bash tool calls** (nohup/setsid/disown/`&` all get reaped; even `run_in_background` rnsd exits 1 — likely the shared-instance socket bind is blocked in this sandbox). Pre-reboot it ran once and showed the interface; post-reboot it won't. **Do not rely on rnsd for radio bring-up here — drive the radio directly over KISS** (config cmds + `CMD_RADIO_STATE=1`) from a single Python process instead.
- **Long/interactive Bash calls get killed by the harness with "exit 1, no output"** (buffered stdout lost). Keep device-interaction scripts SHORT (≤~8s foreground), use `python -u`, and write results to a file you `cat` in a separate call. `run_in_background` for python also failed to persist here. The reliable pattern this session was a ~7s foreground window script (see `/tmp/win.py` style / `rx_monitor.py`).
- The device re-enumerates (port mtime bumps) after: hash write (`CMD_FW_HASH` → hard_reset), DFU flash, provisioning. A plain `rnodeconf -i` does NOT reboot it. Opening at 115200 does NOT reset it (only the 1200-baud touch does).

## Next steps (where this session left off)
1. **FIRST honest RX test (radio is now armable):** drive KISS bring-up to arm the radio (`rx_diag.py`, or the short-window pattern), confirm `radio_online=1`, then have the user spam announces from the Heltec and watch `stat_rx`/`stat_tx`/`CMD_DATA`. All prior RX tests had the radio OFF (hash gate), so RX may simply work now — verify before assuming any RX driver bug. NOTE: arming requires `hw_ready=true`; if the radio won't arm again, re-run `hash_sync.py` (a fresh nrfutil flash will re-break the gate) and check `bt_ready`.
2. Based on that result:
   - Both zero → look at IRQ/interrupt attach correctness again, or whether the chip is actually being put in continuous RX mode at all (`lr11xx_radio_set_rx` call in `receive()`), or a genuine RF-front-end/antenna issue.
   - `stat_rx` increments, `stat_tx` (CRC errors) ≈ all of it → modulation parameters (SF/BW/CR) mismatch between what's configured and what's actually applied at the time RX starts, or a frequency error large enough to fail CRC consistently.
   - `stat_rx` increments with clean (non-CRC-error) packets → bug is upstream of the radio driver (KISS framing inside `handleDio0Rise()`/`_onReceive` callback chain, or in RNS itself) — much narrower.
3. Remove the temporary `stat_rx`/`stat_tx` diagnostic once RX is confirmed working, since they're repurposing fields that might get legitimate uses later.
4. Update `/home/idan/.claude/projects/-home-idan-Downloads-test/memory/project_t1000e_rnode_firmware.md` with the final RX root cause once found.

---

## Resolved/historical (kept for reference, no longer blocking)

These items from an earlier session are now resolved — board model detection works, EEPROM validity works, BLE reconnect works. Original notes below for historical context only.

### BLE Reconnection Fix (`Bluetooth.h`)
`bt_periph_connect_callback()` calls `conn->requestPairing()` on every connect; `bt_disconnect_callback()` restarts advertising. Root cause of the original "pairs but never reconnects" symptom was actually a stale BlueZ bond/GATT-handle cache on the host from repeated reflashing during testing, not a firmware bug — fixed by `bluetoothctl remove <addr>` + fresh pairing. If BLE reconnect ever appears broken again after a reflash, suspect the host-side bond cache before the firmware.

### EEPROM Validation Fix (`Utilities.h`)
Added `PRODUCT_T1000E` (0x1E) to `eeprom_product_valid()` and `MODEL_B5` (0xB5) to `eeprom_model_valid()`.

### rnodeconf.py patches (in `/home/idan/Downloads/venvs/rns/lib/python3.14/site-packages/RNS/Utilities/rnodeconf.py` — venv-local, lost if venv is reinstalled)
- `ROM` class constants: `PRODUCT_T1000E=0x1E`, `BOARD_T1000E=0x52`, `MODEL_B5=0xB5`
- Model dict entry `0xB5` → `rnode_firmware_t1000e.zip`
- `RNode.request_firmware_hash()` method + `RNode.enable_bluetooth()` method
- Post-flash provisioning (both `-r`/bootstrap and `-u`/update paths) use `request_firmware_hash()` to re-sync the on-device hash gate instead of trusting a host-computed `.bin` hash (which covers the wrong flash region length for this bootloader)
- Automatic `enable_bluetooth()` call after EEPROM bootstrap for `BOARD_T1000E`, since a fresh unit has BT off until a host explicitly enables it
