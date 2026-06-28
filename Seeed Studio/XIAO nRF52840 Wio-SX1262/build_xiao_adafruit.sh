#!/usr/bin/env bash
# Build the XIAO nRF52840 + Wio-SX1262 RNode firmware with the ADAFRUIT nRF52
# core (S140 7.3.0), the "Meshtastic way" — the Seeeduino core's flash/SD path
# hangs in Bluefruit bond_init on this board. Requires one-time toolchain setup
# (done by setup_adafruit_core() below): v7 ldscript, XIAO variant, board entry.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/RNode_Firmware"
BUILDROOT="$(cd "$HERE/../SENSECAP T1000-E/arduino_build" && pwd)"
BIN="$BUILDROOT/bin/arduino-cli"
ADA="$BUILDROOT/data/packages/adafruit/hardware/nrf52/1.7.0"
SEEED="$BUILDROOT/data/packages/Seeeduino/hardware/nrf52/1.1.13"
DST="/tmp/RNode_Firmware_XIAO"
CFG="/tmp/acli_ada.yaml"
VENV_BIN="$HOME/Downloads/venvs/rns/bin"
FQBN="adafruit:nrf52:xiaoseeed"
BOARD_MODEL="0x53"

setup_adafruit_core() {
  # v7 ldscript (start 0x27000 for S140 7.3.0)
  [ -f "$ADA/cores/nRF5/linker/nrf52840_s140_v7.ld" ] || \
    sed 's/0x26000/0x27000/g' "$ADA/cores/nRF5/linker/nrf52840_s140_v6.ld" > "$ADA/cores/nRF5/linker/nrf52840_s140_v7.ld"
  # XIAO variant
  [ -d "$ADA/variants/Seeed_XIAO_nRF52840" ] || cp -r "$SEEED/variants/Seeed_XIAO_nRF52840" "$ADA/variants/Seeed_XIAO_nRF52840"
  # board entry (see boards.txt; appended once)
  grep -q "^xiaoseeed.name=" "$ADA/boards.txt" || { echo "ERROR: add xiaoseeed board entry to $ADA/boards.txt"; exit 1; }
  # InternalFS NVMC patch: the stock flash HAL only uses sd_flash_* and blocks
  # forever when the SoftDevice is enabled but no Bluefruit task dispatches its
  # events (our BLE-disabled build). The patch adds a direct-NVMC path taken
  # when the SoftDevice is off (sd_softdevice_disable() is called at boot).
  # If the adafruit core is ever reinstalled, this patch must be re-applied.
  local FLASHC="$ADA/libraries/InternalFileSytem/src/flash/flash_nrf5x.c"
  grep -q "rnode_nvmc_erase" "$FLASHC" || { echo "ERROR: re-apply the NVMC flash patch to $FLASHC (see AGENTS.md)"; exit 1; }
  # XIAO variant LF-clock patch: the Seeed XIAO nRF52840 has a 32kHz crystal,
  # so USE_LFXO is required. Stock variant.h defines USE_LFRC, which wedges
  # sd_softdevice_enable (boot hangs blue-only). Meshtastic's kit variant uses
  # USE_LFXO. If the adafruit core is reinstalled, re-apply this.
  local VARH="$ADA/variants/Seeed_XIAO_nRF52840/variant.h"
  grep -q "^#define USE_LFXO" "$VARH" || { echo "ERROR: re-apply the USE_LFXO patch to $VARH (see AGENTS.md)"; exit 1; }
}

setup_adafruit_core

# arduino-cli needs a config pointing at the bundled data/downloads/user dirs,
# otherwise it looks in the default location and reports "platform not installed"
# even though the adafruit nRF52 core lives under arduino_build/data.
cat > "$CFG" <<EOF
board_manager:
    additional_urls:
        - https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
        - https://adafruit.github.io/arduino-board-index/package_adafruit_index.json
directories:
    data: $BUILDROOT/data
    downloads: $BUILDROOT/downloads
    user: $BUILDROOT/user
EOF

echo ">> Reconstructing sketch from $SRC ..."
rm -rf "$DST"; mkdir -p "$DST"
cp -a "$SRC/." "$DST/"
mv "$DST/RNode_Firmware.ino.cpp" "$DST/$(basename "$DST").ino"
find "$DST" -type f \( -name '*.ino' -o -name '*.h' -o -name '*.cpp' -o -name '*.c' \) -exec sed -i '/^#line /d' {} +
rm -rf "$DST/.git" "$DST/.github" "$DST/Documentation" "$DST/Release" "$DST/Console"

echo ">> Compiling ($FQBN, -DBOARD_MODEL=$BOARD_MODEL, S140 7.3.0) ..."
export PATH="$VENV_BIN:$PATH"
"$BIN" --config-file "$CFG" compile --fqbn "$FQBN" -e \
  --build-property "compiler.cpp.extra_flags=-DBOARD_MODEL=$BOARD_MODEL" \
  "$DST"
echo ">> DONE."
ls -la "$DST/build/adafruit.nrf52.xiaoseeed/"*.hex 2>/dev/null
