---
name: firmware-distribution-push-workflow
description: How to distribute a new T1000E firmware build across all channels when pushing
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 8b99a2c2-1bf8-4a91-bb86-75b39e8adbd6
---

When pushing a new T1000E firmware build, update ALL distribution channels, not just the branch:

1. **Drop-in zip on `main`** — overwrite `firmware/rnode_firmware_t1000e.zip` (and the production `Seeed Studio/SENSECAP T1000-E/rnode_firmware_seeed_t1000e_lr1110.zip`), commit, and `git push origin main`. This is what the web flasher pulls (its `firmware_url` is the raw `main` path), so a branch push alone makes the flasher serve it.
2. **GitHub Release asset** — also refresh the Release's attached zip. A `git push` does NOT touch Release assets. Either:
   - clobber the asset on the existing release: `gh release upload <tag> firmware/rnode_firmware_t1000e.zip --clobber`, OR
   - **create a NEW release** (new tag) if the changes are important / the firmware is meaningfully different from the current release — don't reuse a tag for a substantively different build.

**Why:** A branch push updates only branch files. The "drop-in zip" (web flasher, fetched from raw `main`) and the GitHub *Release asset* are independent channels — the user caught the Release sitting stale after a push that updated only `main`. The flasher Pages site (separate repo `idan2025/rnode-flasher`) needs no change; it resolves firmware at runtime from raw `main`.

**How to apply:** After any firmware rebuild, do both #1 and #2. Verify each: raw `main` zip hash via `curl -sL <raw-url> | sha256sum`; Release asset server-side via the API (bypasses the download CDN, which lags): `gh api -H "Accept: application/octet-stream" repos/idan2025/Rnode_Firmware/releases/assets/<id> | sha256sum` and check `.size`. The `releases/download/...` browser URL stays cached on GitHub's CDN for a few minutes — don't trust it as proof; trust the API.

Build/flash steps: [[t1000e-build-flash-recipe]]. Current open fix: [[ble-stops-on-usb-while-serial]].
