#!/usr/bin/env bash
# One-command (re)build of the T1000-E RNode firmware.
#
# Why this exists: the buildable Arduino sketch lives in /tmp (wiped on reboot),
# and the tracked source (RNode_Firmware_recovered/) is the Arduino-PREPROCESSED
# form (RNode_Firmware.ino.cpp with cosmetic `#line` directives). This script
# reconstructs a clean sketch in /tmp and compiles it, so you never redo the
# fiddly reconstruction by hand. Output DFU zip path is printed at the end.
#
# Usage:  ./build_t1000e.sh           # reconstruct from tracked source + build
#         ./build_t1000e.sh --from-snapshot   # restore the saved /tmp snapshot instead
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/RNode_Firmware_recovered"
BUILDROOT="$HERE/arduino_build"
BIN="$BUILDROOT/bin/arduino-cli"
DST="/tmp/RNode_Firmware"
CFG="/tmp/acli.yaml"
VENV_BIN="$HOME/Downloads/venvs/rns/bin"   # adafruit-nrfutil lives here (needed by arduino-cli)

if [[ "${1:-}" == "--from-snapshot" ]]; then
  echo ">> Restoring sketch from snapshot tarball..."
  rm -rf "$DST"
  tar -xzf "$HERE/t1000e_ready_sketch.tar.gz" -C /tmp
else
  echo ">> Reconstructing buildable sketch from $SRC ..."
  rm -rf "$DST"; mkdir -p "$DST"
  cp -a "$SRC/." "$DST/"
  # the main file is the preprocessed .ino.cpp -> make it the sketch .ino so
  # arduino-cli scans its #includes for library auto-discovery
  mv "$DST/RNode_Firmware.ino.cpp" "$DST/RNode_Firmware.ino"
  # strip cosmetic `#line ".../tmp/..."` directives (they confuse the preprocessor)
  find "$DST" -type f \( -name '*.ino' -o -name '*.h' -o -name '*.cpp' -o -name '*.c' \) \
    -exec sed -i '/^#line /d' {} +
  # drop non-source dirs
  rm -rf "$DST/.git" "$DST/.github" "$DST/Documentation" "$DST/Release" "$DST/Console"
fi

# arduino-cli config: point data/downloads/user at the bundled toolchain
cat > "$CFG" <<EOF
board_manager:
    additional_urls:
        - https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
directories:
    data: $BUILDROOT/data
    downloads: $BUILDROOT/downloads
    user: $BUILDROOT/user
EOF

echo ">> Compiling (FQBN Seeeduino:nrf52:tracker_t1000_e_lorawan, -DBOARD_MODEL=0x52) ..."
export PATH="$VENV_BIN:$PATH"

# Default build = continuous RX (reliable, no missed packets). Pass --low-power
# to build the experimental low-power variant: the LR1110 runs a CAD/sleep RX
# duty-cycle loop and the MCU WFI-sleeps between packets. ONLY for peers that
# transmit a long preamble, otherwise CAD can miss packets. See AGENTS.md.
LP_FLAG=""
if [[ "${1:-}" == "--low-power" || "${2:-}" == "--low-power" ]]; then
  echo ">> LOW_POWER_RX=1 (CAD duty-cycle RX -- read the trade-off in AGENTS.md)"
  LP_FLAG=" -DLOW_POWER_RX"
fi

"$BIN" --config-file "$CFG" compile \
  --fqbn Seeeduino:nrf52:tracker_t1000_e_lorawan -e \
  --build-property "compiler.cpp.extra_flags=-DBOARD_MODEL=0x52${LP_FLAG}" \
  "$DST"

ZIP="$DST/build/Seeeduino.nrf52.tracker_t1000_e_lorawan/RNode_Firmware.ino.zip"
echo ">> DONE. DFU package:"
echo "   $ZIP"
ls -la "$ZIP"
