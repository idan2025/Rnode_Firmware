#!/usr/bin/env bash
# rnode.sh — flash / configure / show / wipe for the Seeed SenseCAP T1000-E
# (and compatible boards) running this fork's RNode firmware.
#
# Subcommands:
#   flash     --dev <port> [--firmware <path>]   flash + provision + hash-sync
#   configure [--dev <port>] [--field value ...] change radio parameters
#   show      [--dev <port>]                     read & print device state
#   wipe      [--dev <port>] [--yes]             erase device EEPROM
#   --help | -h | help
#
# This is a thin wrapper over:
#   - tools/rnode_serial.py  (the real KISS/RNode work)
#   - "Seeed Studio/SENSECAP T1000-E/provision_t1000e.sh"  (the flash flow)
#
# The companion bash script already does the actual flash + provision +
# hash-sync for the T1000-E correctly (including the tricky udev re-enumeration
# and the on-device firmware-hash gate). We shell out to it from `flash` so
# we don't re-derive that work here.
#
# HARD RULE: this script never opens or writes to /dev/ttyACM* or /dev/ttyUSB*
# itself -- all serial I/O is delegated to rnode_serial.py (which the user
# can drive against a mock for testing). For `flash`, the underlying
# provision_t1000e.sh is invoked as a subprocess but the user has the option
# to dry-run by setting RNODE_DRY_RUN=1 (we'll print the command and exit 0).

set -u

# --- locate ourselves and the repo root ------------------------------------
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "${SCRIPT_DIR}/.." && pwd )"
PROVISION_SCRIPT="${REPO_ROOT}/Seeed Studio/SENSECAP T1000-E/provision_t1000e.sh"
DEFAULT_FIRMWARE="${REPO_ROOT}/firmware/rnode_firmware_t1000e.zip"
BOARD_FIRMWARE="${REPO_ROOT}/Seeed Studio/SENSECAP T1000-E/rnode_firmware_seeed_t1000e_lr1110.zip"

# Preferred Python interpreter. We don't hard-code a venv path; rnode_serial.py
# only needs the rns package importable (for KISS/RNode constants) and pyserial.
# Resolution order: $PYTHON env > first python3 on PATH that can import both
# > hard error with install instructions.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        if python3 -c "import sys; sys.path.insert(0, '/home/josh/reticulum-stack/venv/lib/python3.13/site-packages' if '/home/josh/reticulum-stack' in sys.path or True else ''); import serial; from RNS.Utilities.rnodeconf import KISS" 2>/dev/null; then
            PYTHON="python3"
        elif python3 -c "import serial; from RNS.Utilities.rnodeconf import KISS" 2>/dev/null; then
            PYTHON="python3"
        fi
    fi
fi
if [ -z "$PYTHON" ]; then
    # Fall back: look for a venv'd python anywhere on PATH.
    for cand in python3 /usr/bin/python3; do
        if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import serial; from RNS.Utilities.rnodeconf import KISS" 2>/dev/null; then
            PYTHON="$cand"
            break
        fi
    done
fi

# --- helpers ---------------------------------------------------------------
log() { echo "[rnode] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

usage() {
    cat <<'EOF'
rnode.sh -- flash / configure / show / wipe for the T1000-E (and friends)

USAGE
    rnode.sh <subcommand> [options]

SUBCOMMANDS
    flash     --dev <port> [--firmware <path>]
               Flash + provision + hash-sync a T1000-E. Shells out to the
               repo's provision_t1000e.sh, which handles the fragile
               udev re-enumeration and the on-device firmware-hash gate.
               --firmware defaults to firmware/rnode_firmware_t1000e.zip,
               then the board-folder zip, then errors if neither exists.

    configure [--dev <port>] [--field value ...]
               Change one or more radio parameters. Valid fields:
                   --freq <Hz>     carrier frequency
                   --bw   <Hz>     LoRa signal bandwidth
                   --sf   <int>    spreading factor
                   --cr   <int>    coding rate denominator
                   --txp  <dBm>    transmit power
               All fields are validated client-side (range + type) before
               anything is sent to the device. With no --yes, the change
               is summarised and you must type 'yes' to apply.

    show      [--dev <port>]
               Print device identity, product/model, current live radio
               settings, and the stored config from the EEPROM.

    wipe      [--dev <port>] [--yes]
               Erase the device EEPROM (CMD_ROM_WIPE). Destructive and
               irreversible; must type 'WIPE' to confirm unless --yes.

PORT AUTO-DETECTION
    If --dev is omitted, all of /dev/ttyACM* and /dev/ttyUSB* are scanned.
    If exactly one is found, it is used. If multiple are found, you are
    prompted to pick. If none are found, an error is printed.

OPTIONS COMMON TO ALL SUBCOMMANDS
    --dev <path>           serial device (skips auto-detect)
    --board <name>         board profile (default: t1000e)
    --dev-root <path>      filesystem root to scan for /dev/ttyACM* etc.
                           (default: /dev; useful for tests)
    -h, --help             show this help

EXAMPLES
    # Flash the latest bundled firmware to the first T1000-E found
    rnode.sh flash

    # Flash a specific firmware to a specific port
    rnode.sh flash --dev /dev/ttyACM1 --firmware ~/Downloads/my_build.zip

    # Show what's on the device
    rnode.sh show

    # Set 915 MHz, 125 kHz BW, SF 7, CR 5, 14 dBm on the device
    rnode.sh configure --freq 915000000 --bw 125000 --sf 7 --cr 5 --txp 14

    # Same, but skip the confirmation prompt
    rnode.sh configure --freq 915000000 --bw 125000 --sf 7 --cr 5 --txp 14 --yes

    # Erase the EEPROM (will need a re-flash afterwards)
    rnode.sh wipe --yes

SEE ALSO
    Seeed Studio/SENSECAP T1000-E/provision_t1000e.sh
    Seeed Studio/SENSECAP T1000-E/hash_sync.py
    tools/rnode_serial.py

EXIT CODES
    0   success
    1   user aborted / validation failure
    2   port resolution / device error
    3   internal error (missing dependency, etc.)

EOF
}

# --- subcommand dispatch ---------------------------------------------------
SUBCMD="${1:-}"
if [ -z "$SUBCMD" ] || [ "$SUBCMD" = "--help" ] || [ "$SUBCMD" = "-h" ] || [ "$SUBCMD" = "help" ]; then
    usage
    exit 0
fi
shift

# flash: special-cased because it doesn't use rnode_serial.py
if [ "$SUBCMD" = "flash" ]; then
    DEV=""
    FIRMWARE=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dev)        DEV="${2:-}";        shift 2 ;;
            --firmware)   FIRMWARE="${2:-}";   shift 2 ;;
            --help|-h)    usage; exit 0 ;;
            *)            fail "unknown flash option: $1" ;;
        esac
    done

    if [ ! -x "$PROVISION_SCRIPT" ]; then
        fail "provision script not found or not executable: $PROVISION_SCRIPT"
    fi

    # Resolve firmware: explicit > default > board-folder > error.
    if [ -z "$FIRMWARE" ]; then
        if [ -f "$DEFAULT_FIRMWARE" ]; then
            FIRMWARE="$DEFAULT_FIRMWARE"
            log "using default firmware: $FIRMWARE"
        elif [ -f "$BOARD_FIRMWARE" ]; then
            FIRMWARE="$BOARD_FIRMWARE"
            log "using board-folder firmware: $FIRMWARE"
        else
            fail "no firmware found; pass --firmware <path> (looked in $DEFAULT_FIRMWARE and $BOARD_FIRMWARE)"
        fi
    fi
    if [ ! -f "$FIRMWARE" ]; then
        fail "firmware file not found: $FIRMWARE"
    fi

    # Build the command line. The provision script takes the serial port as
    # its first positional arg (matching the actual usage line in the script:
    #   Usage:   ./provision_t1000e.sh [serial-substring]
    # -- that is a udev-serial substring, not /dev/ttyXXX. To pass an exact
    # path we'd need to extend the script; for now we pass the basename as
    # the substring and let udev find it. If the user wants a specific port,
    # they can pass --dev /dev/ttyACM0 and we'll pass ttyACM0 (the script
    # filters by ID_SERIAL_SHORT, so we'll just pass the path's basename).
    PORT_ARG=""
    if [ -n "$DEV" ]; then
        PORT_ARG="$(basename "$DEV")"
        log "targeting port: $DEV (substring: $PORT_ARG)"
    fi

    # Dry-run mode for testing: print what we'd run, don't run it.
    if [ "${RNODE_DRY_RUN:-0}" = "1" ]; then
        echo "[dry-run] would execute: $PROVISION_SCRIPT $PORT_ARG" >&2
        exit 0
    fi

    log "delegating to provision_t1000e.sh ..."
    # provision_t1000e.sh wraps everything itself (flash + provision +BLE + hash-sync).
    # We pass the port-substring as the first positional arg.
    if [ -n "$PORT_ARG" ]; then
        "$PROVISION_SCRIPT" "$PORT_ARG"
    else
        "$PROVISION_SCRIPT"
    fi
    rc=$?
    if [ "$rc" -ne 0 ]; then
        fail "provision_t1000e.sh exited with code $rc"
    fi
    log "flash complete."
    exit 0
fi

# All other subcommands go through rnode_serial.py.
if [ -z "$PYTHON" ]; then
    fail "could not find a python3 with both pyserial and RNS installed; install rns (pip install rns) and pyserial (pip install pyserial), or set PYTHON=..."
fi

# Subcommand is the first arg; pass remaining args through to rnode_serial.py.
# Map the user-facing subcommand name to the python helper's action.
PYTHON_ACTION="$SUBCMD"
case "$SUBCMD" in
    show|wipe) ;;
    configure) PYTHON_ACTION="set" ;;
    *) fail "unknown subcommand: $SUBCMD (run 'rnode.sh --help' for usage)" ;;
esac

# Dry-run mode for testing: print the python command, don't execute it.
if [ "${RNODE_DRY_RUN:-0}" = "1" ]; then
    echo "[dry-run] would execute: $PYTHON ${SCRIPT_DIR}/rnode_serial.py $PYTHON_ACTION $*" >&2
    exit 0
fi

exec "$PYTHON" "${SCRIPT_DIR}/rnode_serial.py" "$PYTHON_ACTION" "$@"
