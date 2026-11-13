# WZRD input, network audio and audio swell: what is still open

> For a gig on 21 Nov 2026. Everything an agent could build on the Mac is built; what remains
> needs the Pi, the rig and Xander. This repo is public: no hostnames, IPs or passwords in
> this file or in any commit (they live in the gitignored `cmds.txt` and `.env`).
> Counterpart plans on the Mac: `~/Documents/GitHub/Gigabrain/VJ/WZRD/docs/TODO/led-sync.md`
> (WZRD sends the wall's colours; it owns contract C-LED) and
> `~/Documents/GitHub/Realtime_PyAudio_FFT/docs/TODO/audio-level.md` (the audio server).
>
> **Settled by Xander (2026-11-13), do not reopen:** one audio server on the Mac at the gig,
> with network audio on the Pi switched by `LEDCTL_AUDIO_SOURCE=network` in the Pi's `.env`;
> onsets travel as single packets with no redundancy, and an event delay is built only if
> rehearsal shows the LEDs leading the wall; WZRD's master brightness and saturation couple to
> the LEDs, each switchable in the modal; `spread` defaults to 1; the render loop wakes on
> onset packets only; the packet allowlist is built but empty at the gig; signal lost means
> hold; Xander turns the public Tailscale Funnel off himself on the Pi day.

## Open: the Pi day (every step needs Xander present or his go-ahead)

An agent never does these on its own: connect to the Pi, restart or reboot it, change a
systemd unit, edit its `.env`, change Tailscale or WLED settings, push, or touch the strip
config.

1. **Before pulling the new code, add `LEDCTL_PASSWORD=...` to the Pi's `.env`, with a new
   password, not the previous one.** Since the password moved out of the tracked config,
   ledctl refuses to start when the variable is missing, and the LEDs stay dark.
2. **Switch to network audio:** add `LEDCTL_AUDIO_SOURCE=network` to the same `.env`. Removing
   the line returns the Pi to its own microphone and server.
3. **Stop the network watchdog for the gig:** `sudo systemctl disable --now net-watchdog`.
   It reboots the Pi after 10 minutes without internet, again after every boot, and keeps
   forcing NetworkManager onto the summer venue's WiFi. Re-enable it for unattended installs.
4. **Turn WiFi power saving off on the Pi** (`iw dev wlan0 get power_save`; NetworkManager
   `wifi.powersave = 2` makes it stick). With power saving on, inbound packets are held and
   delivered in bursts, which is exactly the delay `max_gap_ms` shows.
5. **Network:** Mac and Pi on the same phone hotspot. Check that no firewall on the Pi blocks
   UDP 9000 and 9100. From the Mac, on that hotspot, `ping <pi-name>.local`: a phone hotspot
   does not always pass mDNS. If it answers, use the name; if not, use the Pi's address on the
   hotspot, read from the phone. Type the same target in two places: `osc.destinations` (port
   9000) in the Mac audio server's gitignored `configs/main.yaml`, and WZRD's LED link target
   (port 9100). Edit `main.yaml` only while WZRD is closed: WZRD starts that server, and a
   running server rewrites the file from memory, which would discard the edit.
6. **Check, with `fake_wzrd.py` first and WZRD second:** the modal shows live with 60 packets
   a second (about 30 while WZRD's OUTPUT switch is off) and a largest gap under 30 ms; audio
   shows about 187 `lmh` packets a second; `frames_sent` keeps climbing.
7. **Frame rate, never measured on a Pi:** `engine.fps` holds 60 with audio flowing (the
   render loop now schedules each frame from when the previous one was due), and with
   `listen` on against off (the recolour cost on a Pi 4 is unknown; on the Mac it is about
   0.1 ms). On the Mac the rate dipped to 55-57 when the machine was busy, with `listen` on
   and off alike; watch whether the Pi shows the same.
8. **Rehearsal settings:** `level_full` from the level shown while typical content plays; the
   ledctl brightness master low enough that the LEDs do not wash out the projection; for the
   audio swell, read `level` beside the slider at soundcheck and set `quiet` / `loud` in the
   `audio_swell:` block (room sound through the Mac's microphone read 0.18 to 0.64, so the
   defaults 0.33 / 0.80 will likely need lowering); if beat flashes on the LEDs visibly lead
   the wall, build the event delay under "Later".
9. **Xander turns the public Tailscale Funnel off for the gig** (`tailscale funnel status`
   shows what is public). An agent never does this.
10. **While there, read only:** `journalctl -u ledctl | grep audio-server`. The Pi config
    starts its server with `--no-fft`, a flag that exists nowhere in the audio repo's history;
    if the installed server rejects it, standalone audio is already dead and the spawn fix
    under "Later" becomes urgent for standalone nights.

*Done when* the LEDs follow WZRD's wall and its master sliders for 30 minutes with a steady
60 fps and no drop in the `frames_sent` rate.

## Open: not checked on the Mac

- The mobile page on a real phone (it was checked in a desktop-width browser window).
- The packet allowlist from a second machine (checked with a loopback sender only).
- `max_gap_ms` for both links over WiFi.

## Later, not for the gig

- **Event delay.** `audio_server.event_delay_ms` (default 0): the listener timestamps each
  onset and `Engine._build_audio_view` releases it when due. Build it only if rehearsal shows
  the LEDs leading the wall.
- **Audio-server spawn fixes for standalone mode with the current server:** the binary is
  `<audio repo>/engine/target/release/audio-server` with no arguments and finds its config by
  walking up from its own path; `--no-fft` does not exist (switch the FFT off over the
  server's WebSocket once it answers); restart the server on exit with a backoff. Needs
  `LimitRTPRIO` and write access to the audio repo's `configs/` in `ledctl.service`.
- A line in the LLM prompt (`surface/prompt.py`) while `listen` is on, saying that hues only
  pick wall colours.
- Controls for `quiet` and `loud` in the UI; an audio-source switch in the UI.
- Per-zone colours, once WZRD sends them.

## Shipped

The audio bridge (onsets from `/audio/onset/low`, `/audio/level`, the network source, the
allowlist, render wake on onsets only, link stats); the config writer that keeps every block,
`auth` included; the output-stage hook in `surface/runtime.py`; the `wzrd` module
(`src/ledctl/wzrd/`: listener, smoothed state, recolour stage, `/wzrd` API, settings in the
gitignored `config/wzrd_input.yaml`); the WZRD modal in both front-ends (`src/web/lib/wzrd.js`);
`scripts/fake_wzrd.py`; the `audio_swell` master. Checked on the Mac against the real audio
server and the real WZRD engine.

Done — see [reference/wzrd-input.md](../reference/wzrd-input.md).
