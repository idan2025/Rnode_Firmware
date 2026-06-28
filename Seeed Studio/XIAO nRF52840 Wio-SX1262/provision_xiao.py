#!/usr/bin/env python3
"""Provision the XIAO nRF52840 + Wio-SX1262 RNode over KISS WITHOUT reflashing.

rnodeconf's -r/--autoinstall paths are keyed to its built-in product DB (which
does not include PRODUCT_XIAO_NRF52 = 0x21) and also sign the EEPROM. The
firmware's hw_ready gate, however, only needs: lock byte + valid product/model/
hwrev + MD5 checksum over the 11-byte info region + a matching firmware hash
(device_init). No signature is required to arm the radio.

This writes the info region, the MD5 checksum, a sane default 915 MHz config and
CONF_OK via CMD_ROM_WRITE (0x52), sets the lock byte LAST, then resets. After
this, run hash_sync.py --write to sync the firmware-hash gate (device_init), and
the radio will auto-arm on the next boot.
"""
import serial, time, sys, hashlib, struct

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_ROM_WRITE, CMD_RESET = 0x52, 0x55

# ROM map (ROM.h)
ADDR_PRODUCT, ADDR_MODEL, ADDR_HW_REV = 0x00, 0x01, 0x02
ADDR_SERIAL, ADDR_MADE, ADDR_CHKSUM   = 0x03, 0x07, 0x0B
ADDR_INFO_LOCK = 0x9B
ADDR_CONF_SF, ADDR_CONF_CR, ADDR_CONF_TXP = 0x9C, 0x9D, 0x9E
ADDR_CONF_BW, ADDR_CONF_FREQ, ADDR_CONF_OK = 0x9F, 0xA3, 0xA7
INFO_LOCK_BYTE = CONF_OK_BYTE = 0x73

# Identity (Boards.h): BOARD_XIAO_NRF52 product/model
PRODUCT, MODEL, HWREV = 0x21, 0xC0, 0x01
SERIAL = bytes([0xB7, 0xEF, 0x3F, 0x9E])          # from device USB serial
MADE   = struct.pack(">I", int(time.time()))      # 4-byte big-endian timestamp

# Default radio config (902-928 MHz band, conservative TX power)
SF, CR, TXP = 8, 5, 14
BW, FREQ = 125000, 915000000

ser = serial.Serial(PORT, 115200, timeout=0.2)
time.sleep(0.3)

def esc(b):
    if b == FEND: return bytes([FESC, TFEND])
    if b == FESC: return bytes([FESC, TFESC])
    return bytes([b])

def rom_write(addr, byte):
    frame = bytes([FEND, CMD_ROM_WRITE]) + esc(addr) + esc(byte) + bytes([FEND])
    ser.write(frame); ser.flush(); time.sleep(0.02)

# 1) info region (product/model/hwrev/serial/made) -> 11 bytes for the checksum
info = bytes([PRODUCT, MODEL, HWREV]) + SERIAL + MADE   # 3 + 4 + 4 = 11
assert len(info) == 0x0B
for i, b in enumerate(info):
    rom_write(ADDR_PRODUCT + i, b)

# 2) MD5 checksum of the 11 info bytes
chk = hashlib.md5(info).digest()                        # 16 bytes
for i, b in enumerate(chk):
    rom_write(ADDR_CHKSUM + i, b)

# 3) default radio config
rom_write(ADDR_CONF_SF, SF)
rom_write(ADDR_CONF_CR, CR)
rom_write(ADDR_CONF_TXP, TXP & 0xFF)
for i, b in enumerate(struct.pack(">I", BW)):   rom_write(ADDR_CONF_BW + i, b)
for i, b in enumerate(struct.pack(">I", FREQ)): rom_write(ADDR_CONF_FREQ + i, b)
rom_write(ADDR_CONF_OK, CONF_OK_BYTE)

# 4) lock LAST (further info writes are rejected once this is set)
rom_write(ADDR_INFO_LOCK, INFO_LOCK_BYTE)

print("EEPROM written: product=0x%02X model=0x%02X hwrev=%d serial=%s" %
      (PRODUCT, MODEL, HWREV, SERIAL.hex()))
print("  checksum(md5)=%s" % chk.hex())
print("  config: SF%d CR4/%d TXP%ddBm BW%d FREQ%d  CONF_OK set, INFO locked" %
      (SF, CR, TXP, BW, FREQ))

# 5) reset to apply
ser.write(bytes([FEND, CMD_RESET, 0xF8, FEND])); ser.flush(); time.sleep(0.5)
ser.close()
print("Reset sent. Next: run hash_sync.py --write to sync the firmware hash.")
