#!/usr/bin/env bash
# One-command (re)build of the XIAO nRF52840 + Wio-SX1262 RNode firmware.
# Reconstructs a clean Arduino sketch in /tmp from the tracked (preprocessed)
# source and compiles it. Shares the arduino-cli toolchain bundled under the
# T1000-E folder. Prints the output DFU zip path.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/RNode_Firmware"
BUILDROOT="$HERE/../SENSECAP T1000-E/arduino_build"   # shared toolchain
BIN="$BUILDROOT/bin/arduino-cli"
DST="/tmp/RNode_Firmware_XIAO"
CFG="/tmp/acli_xiao.yaml"
VENV_BIN="$HOME/Downloads/venvs/rns/bin"
FQBN="Seeeduino:nrf52:xiaonRF52840"
BOARD_MODEL="0x53"   # BOARD_XIAO_NRF52

echo ">> Reconstructing buildable sketch from $SRC ..."
rm -rf "$DST"; mkdir -p "$DST"
cp -a "$SRC/." "$DST/"
# Arduino requires the main sketch file to match the folder name
mv "$DST/RNode_Firmware.ino.cpp" "$DST/$(basename "$DST").ino"
find "$DST" -type f \( -name '*.ino' -o -name '*.h' -o -name '*.cpp' -o -name '*.c' \) \
  -exec sed -i '/^#line /d' {} +
rm -rf "$DST/.git" "$DST/.github" "$DST/Documentation" "$DST/Release" "$DST/Console"

cat > "$CFG" <<EOF
board_manager:
    additional_urls:
        - https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
directories:
    data: $BUILDROOT/data
    downloads: $BUILDROOT/downloads
    user: $BUILDROOT/user
EOF

echo ">> Compiling (FQBN $FQBN, -DBOARD_MODEL=$BOARD_MODEL) ..."
export PATH="$VENV_BIN:$PATH"
"$BIN" --config-file "$CFG" compile \
  --fqbn "$FQBN" -e \
  --build-property "compiler.cpp.extra_flags=-DBOARD_MODEL=$BOARD_MODEL" \
  "$DST"

ZIP="$DST/build/Seeeduino.nrf52.xiaonRF52840/RNode_Firmware.ino.zip"
echo ">> DONE. DFU package:"
ls -la "$ZIP"
