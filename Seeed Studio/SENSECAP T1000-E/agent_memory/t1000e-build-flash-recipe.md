---
name: t1000e-build-flash-recipe
description: "How to build, flash, and probe the T1000E RNode firmware in this repo/sandbox"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 8b99a2c2-1bf8-4a91-bb86-75b39e8adbd6
---

Source of truth for the T1000E port: `Seeed Studio/SENSECAP T1000-E/`. Editable firmware source is `RNode_Firmware_recovered/` (note: its `RNode_Firmware.ino.cpp` is the Arduino-PREPROCESSED artifact — every file carries a cosmetic leading `#line ".../tmp/RNode_Firmware/..."`). The original `.ino` lived in `/tmp/RNode_Firmware` which gets wiped between sessions.

**Build** (reconstruct a sketch, since `/tmp/RNode_Firmware` is gone):
1. Copy `RNode_Firmware_recovered/` → `/tmp/RNode_Firmware/`.
2. Rename `RNode_Firmware.ino.cpp` → `RNode_Firmware.ino` (library auto-discovery scans the `.ino`'s includes; an empty `.ino` + separate `.cpp` breaks it — Adafruit_LittleFS/InternalFS/TinyUSB won't resolve).
3. Strip cosmetic `#line` directives from all sources: `sed -i '/^#line /d'` — they confuse arduino-cli's preprocessor/include scanner.
4. `rm -rf .git .github Documentation Release Console` (non-source dirs).
5. Compile (toolchain bundled in repo at `arduino_build/`):
```
arduino_build/bin/arduino-cli --config-file /tmp/acli.yaml compile \
  --fqbn Seeeduino:nrf52:tracker_t1000_e_lorawan -e \
  --build-property "compiler.cpp.extra_flags=-DBOARD_MODEL=0x52" /tmp/RNode_Firmware
```
`/tmp/acli.yaml` must set `directories.data`/`downloads`/`user` to `arduino_build/{data,downloads,user}`. `-DBOARD_MODEL=0x52` is REQUIRED (no default). The `HAS_TCXO/HAS_INPUT/HAS_SLEEP redefined` warnings are pre-existing (Boards.h defaults then per-board), harmless. Output: `build/Seeeduino.nrf52.tracker_t1000_e_lorawan/RNode_Firmware.ino.zip`.

**Flash** (venv `~/Downloads/venvs/rns/bin` has adafruit-nrfutil + rnodeconf):
```
python -c "import serial,time; s=serial.Serial('/dev/ttyACM0',1200); time.sleep(0.3); s.close()"  # touch -> bootloader
sleep 2
adafruit-nrfutil --verbose dfu serial --package <.../RNode_Firmware.ino.zip> -p /dev/ttyACM0 -b 115200 --singlebank
```
App-mode by-id = `Seeed_Tracker_T10000_E_LoRaWAN_*`; bootloader-mode by-id = `Seeed_Studio_T1000-E_*`.

**After an nrfutil flash, the on-device firmware-hash gate breaks** (radio won't arm; BLE+serial still work). Restore it: `python hash_sync.py /dev/ttyACM0 --write` (device reboots), then re-check until it prints `MATCH`.

**Probing this device is flaky in the QEMU/sandbox passthrough.** Raw heredoc/`read_diag.py` probes frequently return 0 bytes even when the device is fine — `rnodeconf -i /dev/ttyACM0` is the reliable liveness check. BLE liveness: `bluetoothctl scan le | grep RNode` (advertises as `RNode AA6B`). Always continuously-drain in a tight loop; gaps cause TinyUSB-CDC backpressure that blocks the firmware's `Serial.write()`.

Related: [[ble-stops-on-usb-while-serial]].
