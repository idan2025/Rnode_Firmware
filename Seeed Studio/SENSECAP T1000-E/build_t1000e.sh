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
DST="/tmp/RNode_Firmware"
CFG="/tmp/acli.yaml"
VENV_BIN="$HOME/Downloads/venvs/rns/bin"   # adafruit-nrfutil lives here (needed by arduino-cli)

# Prefer the bundled toolchain (original Linux sandbox layout); fall back to
# a system-installed arduino-cli (e.g. `brew install arduino-cli` + the
# Seeeduino:nrf52 board package) when arduino_build/ isn't present, such as
# on a fresh macOS checkout.
if [[ -x "$BUILDROOT/bin/arduino-cli" ]]; then
  BIN="$BUILDROOT/bin/arduino-cli"
  USE_BUILDROOT_CFG=1
else
  BIN="$(command -v arduino-cli || true)"
  if [[ -z "$BIN" ]]; then
    echo "!! No bundled arduino_build/ and no system arduino-cli on PATH. Install arduino-cli" >&2
    echo "   and the Seeeduino:nrf52 board package, or restore arduino_build/." >&2
    exit 1
  fi
  USE_BUILDROOT_CFG=0
fi

# The Adafruit nRF52 bootloader's uf2/DFU packaging post-build hook shells
# out to `python` (not `python3`); macOS only ships `python3`. Shim it in
# without touching the user's real PATH ordering for anything else.
if ! command -v python >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
  PYSHIM="/tmp/t1000e_pyshim"
  mkdir -p "$PYSHIM"
  ln -sf "$(command -v python3)" "$PYSHIM/python"
  export PATH="$PYSHIM:$PATH"
fi

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
  # strip cosmetic `#line ".../tmp/..."` directives (they confuse the preprocessor).
  # `-i.bak` (suffix glued to -i, no space) is the portable form: both GNU and
  # BSD/macOS sed accept it identically, unlike bare `-i` which BSD sed parses
  # differently.
  find "$DST" -type f \( -name '*.ino' -o -name '*.h' -o -name '*.cpp' -o -name '*.c' \) \
    -exec sed -i.bak '/^#line /d' {} +
  find "$DST" -name '*.bak' -delete
  # drop non-source dirs
  rm -rf "$DST/.git" "$DST/.github" "$DST/Documentation" "$DST/Release" "$DST/Console"
fi

CFG_ARGS=()
if [[ "$USE_BUILDROOT_CFG" == "1" ]]; then
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
  CFG_ARGS=(--config-file "$CFG")
fi
# Else: use the system arduino-cli's own default config (its board/lib
# install locations from `arduino-cli core install` / `lib install`).

echo ">> Compiling (FQBN Seeeduino:nrf52:tracker_t1000_e_lorawan, -DBOARD_MODEL=0x52) ..."
if [[ -d "$VENV_BIN" ]]; then export PATH="$VENV_BIN:$PATH"; fi

# Default build = continuous RX (reliable, no missed packets). Pass --low-power
# to build the experimental low-power variant: the LR1110 runs a CAD/sleep RX
# duty-cycle loop and the MCU WFI-sleeps between packets. ONLY for peers that
# transmit a long preamble, otherwise CAD can miss packets. See AGENTS.md.
LP_FLAG=""
if [[ "${1:-}" == "--low-power" || "${2:-}" == "--low-power" ]]; then
  echo ">> LOW_POWER_RX=1 (CAD duty-cycle RX -- read the trade-off in AGENTS.md)"
  LP_FLAG=" -DLOW_POWER_RX"
fi

"$BIN" "${CFG_ARGS[@]+"${CFG_ARGS[@]}"}" compile \
  --fqbn Seeeduino:nrf52:tracker_t1000_e_lorawan -e \
  --build-property "compiler.cpp.extra_flags=-DBOARD_MODEL=0x52${LP_FLAG}" \
  "$DST"

ZIP="$DST/build/Seeeduino.nrf52.tracker_t1000_e_lorawan/RNode_Firmware.ino.zip"
echo ">> DONE. DFU package:"
echo "   $ZIP"
ls -la "$ZIP"
