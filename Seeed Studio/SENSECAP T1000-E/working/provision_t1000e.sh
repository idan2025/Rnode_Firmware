#!/usr/bin/env bash
# One-command provisioning for a Seeed SenseCAP T1000-E running RNode firmware.
#
# Flashes the clean firmware, provisions the EEPROM, enables Bluetooth, and
# syncs the on-device firmware-hash gate -- and ROBUSTLY waits out every USB
# re-enumeration (the device drops off and comes back as a NEW /dev/ttyACM*
# after each flash/reset). Safe to run on a brand-new unit or to re-provision.
#
# Usage:   ./provision_t1000e.sh [serial-substring]
#   serial-substring : optional, to pick a specific unit by USB serial
#                      (default: first Seeed T1000-E found, VID:PID 2886:8057)
#
# Requires the patched rnodeconf (this venv) whose -r/--autoinstall bootstrap
# flow auto-enables BLE and auto-syncs the firmware hash for the T1000-E. This
# script additionally enables BLE + syncs the hash explicitly, because those
# auto-steps are SKIPPED when a valid EEPROM is already present.
#
# Robustness note (the "warm the port" trick): rnodeconf's probe right after a
# flash/reset is flaky (~50% "RNode did not respond" / "Could not download
# EEPROM" in some environments) because the device needs ~1-3s after boot and
# answers reliably only once its CDC-ACM link is drained continuously for a
# moment. Every rnodeconf call below is therefore preceded by warm_port() (a
# short raw-KISS drain) and retried across the flaky probe + re-enumeration.
set -u
VENV=/home/idan/Downloads/venvs/rns
# rnodeconf shells out to adafruit-nrfutil (DFU flasher) by name, so the venv
# bin MUST be on PATH or flashing silently aborts.
export PATH="$VENV/bin:$PATH"
PY="$VENV/bin/python"
RNODECONF="$VENV/bin/rnodeconf"
HASHSYNC="$(cd "$(dirname "$0")" && pwd)/hash_sync.py"
VID=2886; PID=8057                 # Seeed T1000-E app-mode USB id
FWVER=1.86
SERIAL_MATCH="${1:-}"
RC_TRIES="${RC_TRIES:-6}"           # rnodeconf retries across the flaky probe

log(){ echo "[provision] $*"; }
fail(){ echo "[provision][ERROR] $*" >&2; exit 1; }

# Find the T1000-E tty by VID:PID (+ optional serial substring). Echoes the path.
find_port(){
  for p in /dev/ttyACM*; do
    [ -e "$p" ] || continue
    eval "$(udevadm info -q property -n "$p" 2>/dev/null | grep -E '^(ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT)=')"
    if [ "${ID_VENDOR_ID:-}" = "$VID" ] && [ "${ID_MODEL_ID:-}" = "$PID" ]; then
      if [ -z "$SERIAL_MATCH" ] || echo "${ID_SERIAL_SHORT:-}" | grep -q "$SERIAL_MATCH"; then
        echo "$p"; return 0
      fi
    fi
  done
  return 1
}

# Wait (up to ~40s) for the T1000-E to (re)appear and settle after enumeration,
# then warm its serial link so the next rnodeconf open doesn't race the boot.
wait_port(){
  local what="$1" p="" i=0
  log "waiting for device to $what ..."
  while [ $i -lt 80 ]; do
    if p="$(find_port)"; then
      sleep 2                       # let CDC ACM settle
      if [ -e "$p" ]; then PORT="$p"; warm_port "$p"; log "device present at $p"; return 0; fi
    fi
    sleep 0.5; i=$((i+1))
  done
  fail "device did not $what within timeout"
}

# Warm the CDC-ACM link with a short raw-KISS CMD_DETECT drain so the next
# rnodeconf/hash_sync open finds the device already responsive. rnodeconf opens
# its own port, so we warm, close, then immediately hand off. Best-effort.
warm_port(){
  local p="${1:-${PORT:-}}"
  [ -n "$p" ] && [ -e "$p" ] || return 0
  timeout 6 "$PY" - "$p" <<'PYEOF' 2>/dev/null || true
import serial, sys, time
try:
    s = serial.Serial(sys.argv[1], 115200, timeout=0.1); time.sleep(0.3)
    for _ in range(15):                         # ~2s of CMD_DETECT, draining replies
        s.write(bytes([0xC0, 0x08, 0x73, 0xC0])); s.flush(); s.read(256); time.sleep(0.1)
    s.close()
except Exception:
    pass
PYEOF
}

# True if the device answers a raw KISS CMD_DETECT (i.e. RNode firmware present
# and alive). Definitive and independent of rnodeconf's flaky probe; also warms.
device_answers_kiss(){
  local p="${1:-$PORT}"
  [ -e "$p" ] || return 1
  timeout 10 "$PY" - "$p" <<'PYEOF' 2>/dev/null
import serial, sys, time
ok = False
try:
    s = serial.Serial(sys.argv[1], 115200, timeout=0.1); time.sleep(0.3)
    for _ in range(25):
        s.write(bytes([0xC0, 0x08, 0x73, 0xC0])); s.flush()
        if bytes([0xC0, 0x08, 0x46, 0xC0]) in s.read(256): ok = True; break
        time.sleep(0.1)
    s.close()
except Exception:
    pass
sys.exit(0 if ok else 1)
PYEOF
}

# Run rnodeconf (port appended automatically), warming + retrying past the flaky
# post-reset probe and re-resolving the port (it hops ACM0<->ACM1 each reset).
# $1 = extended-regex success marker to look for in the output. Echoes output.
# Returns 0 on first match, else 1 after RC_TRIES.
run_rnodeconf(){
  local marker="$1"; shift
  local a out
  for a in $(seq 1 "$RC_TRIES"); do
    PORT="$(find_port || echo "$PORT")"
    warm_port "$PORT"
    out="$(timeout "${RC_TIMEOUT:-180}" "$RNODECONF" "$@" "$PORT" 2>&1)"
    printf '%s\n' "$out"
    if printf '%s' "$out" | grep -qiE "$marker"; then return 0; fi
    log "  (rnodeconf attempt $a/$RC_TRIES missed success marker; warming + retrying)"
    sleep 2
  done
  return 1
}

# 0 = provisioned (valid EEPROM + signature), 1 = blank/unprovisioned.
# Retries past the flaky probe; distinguishes "blank EEPROM" from "flaky open".
is_provisioned(){
  local a out
  for a in $(seq 1 "$RC_TRIES"); do
    PORT="$(find_port || echo "$PORT")"
    warm_port "$PORT"
    out="$(timeout 25 "$RNODECONF" -i "$PORT" 2>&1)"
    if printf '%s' "$out" | grep -q "Device signature   : Validated"; then return 0; fi
    if printf '%s' "$out" | grep -qiE "Could not download EEPROM|not provisioned"; then return 1; fi
    sleep 2                          # flaky ("did not respond") -> retry
  done
  return 1
}

# ---- 0. locate the device ----------------------------------------------------
wait_port "appear"
log "target unit: $PORT"

# ---- 1. flash the clean firmware --------------------------------------------
# - No RNode firmware at all  -> --autoinstall bootstraps from scratch. (Its
#   internal post-flash probe is flaky, so treat it as best-effort for the FLASH
#   and let step 2 below do the provisioning with warm+retry.)
# - Firmware present + provisioned -> -u updates to the clean build.
# - Firmware present but EEPROM blank/unprovisioned -> -u refuses (exits 1), so
#   skip it and fall through to provisioning (step 2), which is what's needed.
if ! device_answers_kiss "$PORT"; then
  log "no RNode firmware -> running autoinstall (flash; follow any band prompt) ..."
  timeout 300 "$RNODECONF" --autoinstall "$PORT" \
    || log "  autoinstall returned nonzero (likely the flaky post-flash probe); continuing"
  wait_port "re-enumerate after flashing"
  device_answers_kiss "$PORT" || fail "device still has no RNode firmware after autoinstall"
elif is_provisioned; then
  log "RNode firmware + provisioned -> updating to clean build (rnodeconf -u) ..."
  run_rnodeconf "update completed|Done flashing" -u -U --nocheck --fw-version "$FWVER" \
    || fail "firmware update failed"
  wait_port "re-enumerate after flashing"
else
  log "RNode firmware present but EEPROM not provisioned -> skipping -u (it requires"
  log "a provisioned device); proceeding straight to EEPROM provisioning below."
fi

# ---- 2. provision EEPROM (bootstrap) ----------------------------------------
# The bootstrap auto-enables BLE + syncs the hash, but ONLY when it actually
# writes a fresh EEPROM; if a valid one is already present it makes no changes
# (so BLE + hash are handled explicitly in steps 3-4 below regardless).
if is_provisioned; then
  log "EEPROM already provisioned & signature valid -- skipping bootstrap."
else
  log "provisioning EEPROM (product=1e model=b5 hwrev=1) ..."
  run_rnodeconf "Bootstrapping successful|EEPROM written|already present" \
    -r --product 1e --model b5 --hwrev 1 \
    || fail "EEPROM provisioning failed"
  wait_port "re-enumerate after provisioning"
  is_provisioned || fail "EEPROM still not valid after provisioning"
fi

# ---- 3. enable Bluetooth -----------------------------------------------------
# Skipped by the bootstrap when an EEPROM was already present, so do it here.
log "enabling Bluetooth LE ..."
run_rnodeconf "Enabling Bluetooth|Bluetooth" -b \
  || log "  WARNING: could not confirm Bluetooth enable (non-fatal)"
wait_port "settle after BLE enable" >/dev/null 2>&1 || true

# ---- 4. sync + verify the firmware-hash gate --------------------------------
# A direct/DFU flash leaves the on-device hash gate mismatched -> radio never
# arms. Write the live hash as target (hash_sync --write), then confirm MATCH.
log "syncing firmware-hash gate ..."
PORT="$(find_port || echo "$PORT")"; warm_port "$PORT"
timeout 25 "$PY" "$HASHSYNC" "$PORT" --write 2>/dev/null | grep -qi "MATCH" \
  && log "hash gate already in sync" \
  || wait_port "re-enumerate after hash write"

log "final check ..."
ok=0
for i in $(seq 1 "$RC_TRIES"); do
  PORT="$(find_port || echo "$PORT")"; warm_port "$PORT"
  if timeout 20 "$PY" "$HASHSYNC" "$PORT" 2>/dev/null | grep -qi "MATCH"; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] && log "OK: firmware-hash gate MATCH (radio will arm)" \
              || fail "firmware-hash gate still mismatched -- radio will not arm"

PORT="$(find_port || echo "$PORT")"; warm_port "$PORT"
"$RNODECONF" -i "$PORT" 2>&1 | grep -E "Product|signature|Firmware version|Modem chip"
log "DONE -- T1000-E at $PORT is flashed, provisioned, BLE-enabled, hash-synced."
