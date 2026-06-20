# RNode Firmware — Seeed Studio SenseCAP T1000-E

A working [RNode firmware](https://github.com/markqvist/RNode_Firmware) port for the
**Seeed Studio SenseCAP T1000-E** (Nordic nRF52840 + Semtech **LR1110** LoRa transceiver),
which mainline RNode firmware does not support.

> Web-flash it in your browser — no toolchain needed:
> **https://idan2025.github.io/rnode-flasher/** → pick *Seeed SenseCAP T1000-E* → Flash.

## Layout

```
Seeed Studio/
  SENSECAP T1000-E/
    RNode_Firmware_recovered/   custom firmware source (LR1110 driver + lr11xx SDK)
    rnode_firmware_seeed_t1000e_lr1110.zip   prebuilt production DFU package
    provision_t1000e.sh         one-command flash + EEPROM provision + BLE + hash-sync
    hash_sync.py / read_diag.py / lxmf_live.py   bring-up & diagnostic tools
    AGENTS.md / Result.md       engineering log + results
```

## What was fixed for the LR1110

This port adds a new `lr1110` driver class (mirroring the `sx126x` Stream interface) over
Semtech's `lr11xx_driver`. Hardware bring-up surfaced several non-obvious, LR1110-specific bugs:

- **IRQ pin needs an explicit pulldown** — RX_DONE interrupt never fired otherwise (TX worked, RX dead).
- **RX continuous vs single** — `set_rx` timeout `0` is *single-shot* on the LR11xx; only `0xFFFFFF` is continuous.
- **Carrier-detect (`dcd`)** — latched preamble/header IRQ bits must be cleared, or CSMA sees the channel as permanently busy.
- **Explicit-header RX length cap** — on the LR11xx `pld_len` is the *maximum* accepted RX length; RX must be armed at 255 or large/split packets are silently dropped (this is why short messages worked but a 380-char message never arrived).
- **256-byte RX buffer wrap** — `read_buffer8` reads a linear span; large packets past the wrap point must be read in two halves.
- **PA power tables** — per-dBm duty-cycle/hp_sel lookup from Seeed's BSP (a single fixed bias mis-tunes the PA).
- **Firmware-hash gate** — after any manual flash, re-sync the on-device SHA256 gate or the radio silently won't arm (`hash_sync.py`, automated in the patched `rnodeconf`).

See `Seeed Studio/SENSECAP T1000-E/AGENTS.md` for the full engineering log.

## Build

Toolchain: `arduino-cli` + `Seeeduino:nrf52` core + `adafruit-nrfutil`.

```
arduino-cli compile --fqbn Seeeduino:nrf52:tracker_t1000_e_lorawan -e \
  --build-property "compiler.cpp.extra_flags=\"-DBOARD_MODEL=0x52\"" \
  <sketch-dir>
```

Output is a DFU `.zip`. Flash with the web flasher above, or `rnodeconf -u`, or `adafruit-nrfutil`.

## Provisioning a fresh unit

```
./provision_t1000e.sh            # flash + provision (product=1e model=b5 hwrev=1) + BLE + hash-sync
```

## Credits

Built on [RNode_Firmware](https://github.com/markqvist/RNode_Firmware) by Mark Qvist (MIT/GPL as
per upstream). LR1110 support and T1000-E board config added here. Not affiliated with Seeed Studio
or the upstream RNode project.
