# RNode Firmware — Seeed SenseCAP T1000-E

Turn the tiny **Seeed SenseCAP T1000-E** tracker card into a fully working
[**RNode**](https://unsigned.io/rnode/) for [Reticulum](https://reticulum.network) —
long-range LoRa mesh messaging, paired to your phone (Sideband / Columba) over Bluetooth,
or to a computer over USB.

Mainline RNode firmware doesn't support this device, because the T1000-E uses Semtech's
**LR1110** radio instead of the usual SX126x/SX127x. This repo is a port that adds an
LR1110 driver and a T1000-E board profile so the device just works.

## Flash it in your browser (no tools needed)

👉 **[Open the web flasher](https://idan2025.github.io/rnode-flasher/)**

1. Plug the T1000-E into a Chrome/Edge browser over USB.
2. Pick **Seeed SenseCAP T1000-E** and click **Flash** (the firmware downloads automatically).
3. Then **Provision EEPROM** → **Set Firmware Hash**. That's it.

Prefer the command line? Use `rnodeconf -u` or the bundled `./provision_t1000e.sh`
(flash + provision + BLE + firmware-hash sync in one go) — see [the model folder](Seeed%20Studio/SENSECAP%20T1000-E/).

## Does it actually work? Yes — tested hard

- ✅ Short text messages, both directions
- ✅ A 380-character message (multi-packet *split* transfer)
- ✅ **A 128 KB photo** sent over LoRa to a phone (Columba) — delivered end-to-end in a
  few minutes, rock steady
- ✅ Over **both** USB serial and **Bluetooth LE**

## Why the LR1110 needed real work (vs the SX1262)

The T1000-E's radio is an **LR1110**, which behaves differently from the SX126x family the
firmware was written around. "Drop in the SX1262 driver" does not work — each difference
below was a distinct bug found on real hardware and fixed in `lr1110.cpp`:

| The SX126x firmware assumes… | …but the LR1110 actually | Symptom if not handled |
|---|---|---|
| RX-continuous = timeout `0xFFFFFF` | timeout `0` means **receive one packet then stop** | RX froze after the first packet |
| explicit-header RX **ignores** `pld_len` | uses `pld_len` as the **max accepted length** | short messages worked, large/split ones were silently dropped |
| FIFO read **wraps** in hardware | `ReadBuffer` is **linear**, no wrap at the 256-byte mark | the tail of big packets came back as garbage |
| any latched carrier IRQ is fine | preamble/header IRQ bits **stay latched** until cleared | CSMA saw the channel "busy" forever → TX stalled, messages crawled |
| two-register custom sync word | accepts a **single-byte** LoRa sync word | LR1110 RNodes use the standard private sync word |
| DIO/IRQ line behaves | the IRQ pin needs an explicit **pulldown** | RX_DONE never fired (TX worked, RX dead) |
| one fixed PA bias is fine | needs Seeed's **per-dBm PA tables** | radiated power/range quietly mis-tuned |

Plus an on-device firmware-hash gate that must be re-synced after a manual flash, or the
radio silently refuses to start. Full engineering log:
[`AGENTS.md`](Seeed%20Studio/SENSECAP%20T1000-E/AGENTS.md).

## Plug it in without losing Bluetooth

Two USB-side fixes the mainline firmware's nRF52 path didn't cover for this board:

- **Bluetooth no longer drops when you connect USB power.** The device used to wait at
  boot for a host to open its USB serial port — so plugging into a charger, a power bank,
  a media streamer, or a computer before an app connects (anything that supplies power
  without opening the port) left Bluetooth off until you toggled it by hand. The T1000-E
  now boots straight through, like every other nRF52 RNode.
- **A stalled USB host can't freeze the firmware.** Writing to the USB serial port could
  block forever if a host opened the port but stopped reading it (a hung terminal, the OS
  probing the port on plug-in). That froze the *whole* device — radio and Bluetooth
  included. Serial writes are now bounded, so the firmware keeps running no matter what
  the host does.

Both verified on hardware, and the radio was re-checked afterwards — TX, RX, and a clean
−112 dBm noise floor all still good.

## What's in here

```
Seeed Studio/
  SENSECAP T1000-E/
    RNode_Firmware_recovered/    custom firmware source (LR1110 driver + lr11xx SDK)
    rnode_firmware_seeed_t1000e_lr1110.zip   prebuilt production DFU package
    provision_t1000e.sh          one-command flash + provision + BLE + hash-sync
    build_t1000e.sh              one-command reconstruct-sketch + compile
    hash_sync.py / read_diag.py / lxmf_live.py   bring-up & diagnostic tools
    AGENTS.md / Result.md        engineering log + results
firmware/
  rnode_firmware_t1000e.zip      canonical image the web flasher pulls
```

## Build it yourself

Toolchain: `arduino-cli` + the `Seeeduino:nrf52` core + `adafruit-nrfutil`.

```bash
arduino-cli compile --fqbn Seeeduino:nrf52:tracker_t1000_e_lorawan -e \
  --build-property "compiler.cpp.extra_flags=\"-DBOARD_MODEL=0x52\"" \
  <sketch-dir>
```

The output is a DFU `.zip` — flash it with the web flasher, `rnodeconf -u`, or `adafruit-nrfutil`.

---

<sub>Built on [RNode_Firmware](https://github.com/markqvist/RNode_Firmware) by Mark Qvist.
LR1110 driver and T1000-E board support added by [idan2025](https://github.com/idan2025).
Web flasher is a fork of [Liam Cottle's rnode-flasher](https://github.com/liamcottle/rnode-flasher).
Not affiliated with Seeed Studio or the upstream RNode / Reticulum projects.</sub>
