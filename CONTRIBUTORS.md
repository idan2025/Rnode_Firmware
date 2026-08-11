# Contributors

Thanks to everyone who has improved this fork.

## Seeed SenseCAP T1000-E port

- **idan2025** — LR1110 driver, T1000-E board support, battery-life fixes, USB-CDC fixes,
  firmware-hash gate, build/provision tooling, low-power RX mode.
- **Billy Quilty (bquilty25)** — PR #3: fixed BLE pairing PIN (`123456`) + a pre-existing
  one-byte buffer overflow in the PIN string, MAC-suffix device naming, button-polarity
  fix (active-HIGH pulldown), onboard buzzer support, combined status-LED heartbeat, and
  macOS compatibility for `build_t1000e.sh`. Verified on real hardware.
- **joshbowyer** — PR #4: the `tools/rnode.sh` / `rnode_serial.py` KISS serial CLI
  (flash / configure / show / wipe) and tests, and the idea behind the pre-SYSTEMOFF
  LED-off hygiene fix (prevents a lit LED latching through nRF52 SYSTEMOFF).

## Build / provisioning

- **Abhinav Ramachandran (geckods)** — PR #5: auto-detect the Python venv in
  `provision_t1000e.sh` instead of a hardcoded path, so the script works for other users.

## Upstream

Built on [RNode_Firmware](https://github.com/markqvist/RNode_Firmware) by Mark Qvist.
Not affiliated with Seeed Studio or the upstream RNode / Reticulum projects.