# Gledopto without the Pi: what's left to undo, and what we learned

On 2026-11-16 the Pi was left at home and we tried to drive the Gledopto straight from the Mac.
We gave up and went back to the Pi setup. Everything on the Mac is reverted; **one change on the
Gledopto is still in place** and must be undone before the next gig.

## Still to undo: the Gledopto's Wi-Fi settings

**Pending until the next on-site setup.** The Pi wasn't available on 2026-11-16, so this
couldn't be done then. Do it first thing when the Pi and Gledopto are set up together again.

The phone hotspot (`Xander's Pixel` + its password) was entered in WLED → **Config → WiFi Setup**,
hoping the Gledopto would join it. It never did. The rig expects Wi-Fi off, because WLED shares
one static IP between ethernet and Wi-Fi.

Once the Pi is back on the 15 m ethernet cable:

1. Open the WLED UI through the Pi (`http://<pi>:8080`, or the tailnet route in `reach_the_pi.md`).
2. **Config → WiFi Setup:** empty **Network name (SSID)** and **Password**. Check the static IP is
   still **IP `10.0.0.2`, gateway `10.0.0.1`, subnet `255.255.255.0`**. Save, then power-cycle the
   Gledopto.
3. Verify with the DDP debug recipe in `CLAUDE.md`. `curl -s http://10.0.0.2/json/info` should
   show `"live": true, "lip": "10.0.0.1", "lm": "DDP"`, and the LEDs should show ledctl content.
4. Delete the ⚠️ line pointing here in `CLAUDE.md` (Hardware & on-site operations).

What the SSID field held before 2026-11-16 is unknown (possibly empty).

## Lessons, if the Mac ever has to drive the Gledopto again

- **WLED's fallback hotspot:** SSID `WLED-AP`, password `wled1234`, address `4.3.2.1`. ledctl can
  stream to it: copy `config.pi.yaml`, set the controller `host: 4.3.2.1`, server
  `127.0.0.1`, drop the `auth` block, and use `config.dev.yaml`'s `audio_server` command.
  Streaming worked: the LEDs followed ledctl while the Mac was on `WLED-AP`.
- **`WLED-AP` is unusable while a Wi-Fi network is saved that WLED can't join.** WLED keeps
  retrying that network, which drops the hotspot every 10–30 s and kicks clients off. Clear the
  SSID first so the hotspot stays up.
- **The Mac can't be on `WLED-AP` and online at once** (one Wi-Fi radio). Get internet from the
  phone over USB or Bluetooth tethering instead.
- **The Gledopto never joined the phone hotspot** (2.4 GHz was on; cause not diagnosed).
  A phone's connected-devices list wouldn't show it anyway: Android lists DHCP clients, and the
  Gledopto uses a static IP.
- **Simplest option:** bring the Pi, or a USB-C ethernet adapter. With a cable, give the Mac
  `10.0.0.1/24` on it and use `config.pi.yaml` with `auth` removed.
