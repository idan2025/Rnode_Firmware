#!/usr/bin/env python3
"""Live LXMF endpoint over the T1000E for diagnosing large-message RX.

Brings up Reticulum with the existing RNodeInterface (arms the T1000E radio),
registers an LXMF delivery address, announces it so Columba can reach it, and
prints every received LXMF message (length + preview). RNS is set to VERBOSE so
the RNodeInterface logs each received packet / resource part as they arrive --
so even if a large message never completes, we see how far it got.

Usage:
  python lxmf_live.py                      # receiver: announce + print messages
  python lxmf_live.py announce             # force an immediate announce
"""
import os, sys, time, threading, RNS, LXMF

BASE = os.path.dirname(os.path.abspath(__file__))
IDPATH = BASE + "/lxmf_live_identity"
STORE  = BASE + "/lxmf_live_storage"

RNS.loglevel = RNS.LOG_EXTREME   # raw packet dumps

_rx_count = 0
_tx_count = 0
def _install_rx_probe():
    """Wrap every RNodeInterface.process_incoming/outgoing so we log EVERY raw
    packet the radio delivers (RX) and every packet we transmit (TX), with
    length -- independent of Reticulum addressing. Lets us tell 'radio receives
    nothing' from 'received but not for us', and see whether the T1000E is
    actually transmitting its link/resource responses."""
    global _rx_count
    for iface in RNS.Transport.interfaces:
        if iface.__class__.__name__ == "RNodeInterface":
            orig_in = iface.process_incoming
            def wrapped_in(data, _orig=orig_in):
                global _rx_count
                _rx_count += 1
                print("[%s] >>> RX #%d  len=%d  head=%s" %
                      (time.strftime("%H:%M:%S"), _rx_count, len(data), data[:24].hex()))
                sys.stdout.flush()
                return _orig(data)
            iface.process_incoming = wrapped_in

            orig_out = iface.process_outgoing
            def wrapped_out(data, _orig=orig_out):
                global _tx_count
                _tx_count += 1
                print("[%s] <<< TX #%d  len=%d  head=%s" %
                      (time.strftime("%H:%M:%S"), _tx_count, len(data), data[:24].hex()))
                sys.stdout.flush()
                return _orig(data)
            iface.process_outgoing = wrapped_out
            print("RX+TX probe installed on RNodeInterface[%s]" % getattr(iface, "name", "?"))
            sys.stdout.flush()

def delivery_callback(message):
    ts = time.strftime("%H:%M:%S")
    try:
        content = message.content.decode("utf-8", "replace")
    except Exception:
        content = repr(message.content)
    src = RNS.prettyhexrep(message.source_hash) if message.source_hash else "?"
    print("\n==================== MESSAGE RECEIVED [%s] ====================" % ts)
    print("  from        : %s" % src)
    print("  title       : %s" % (message.title.decode("utf-8","replace") if message.title else ""))
    print("  content len : %d chars" % len(content))
    print("  content     : %s" % (content[:600] + ("..." if len(content) > 600 else "")))
    print("  fields      : %s" % (list(message.fields.keys()) if message.fields else []))
    print("==============================================================\n")
    sys.stdout.flush()

def main():
    reticulum = RNS.Reticulum(configdir=os.path.expanduser("~/.reticulum"))
    RNS.loglevel = RNS.LOG_EXTREME   # force; Reticulum() resets it from config

    if os.path.isfile(IDPATH):
        identity = RNS.Identity.from_file(IDPATH)
    else:
        identity = RNS.Identity()
        identity.to_file(IDPATH)

    router = LXMF.LXMRouter(identity=identity, storagepath=STORE)
    delivery = router.register_delivery_identity(identity, display_name="ClaudeT1000E")
    router.register_delivery_callback(delivery_callback)

    _install_rx_probe()

    addr = RNS.prettyhexrep(delivery.hash)
    print("\n########################################################")
    print("#  LXMF address (send your long message here):")
    print("#      %s" % addr)
    print("########################################################\n")
    sys.stdout.flush()

    # Announce now and then periodically so Columba learns the path.
    def announce_loop():
        while True:
            try:
                router.announce(delivery.hash)
                print("[%s] announced %s" % (time.strftime("%H:%M:%S"), addr)); sys.stdout.flush()
            except Exception as e:
                print("announce error:", e); sys.stdout.flush()
            time.sleep(20)
    threading.Thread(target=announce_loop, daemon=True).start()

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
