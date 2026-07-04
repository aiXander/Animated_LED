# Reaching the Pi from phones — current state and routes forward

Two intertwined problems that came up while planning the on-site setup:

1. Guests' phones (especially Android Chrome) couldn't reach the operator UI on the phone-hotspot network.
2. We're switching from the phone hotspot to a fixed on-site WiFi network and want the Pi to land on the same IP every reboot.

This doc captures what's already in place and the candidate fixes.

---

## 0. WORKING SOLUTION (2026-07-03): Tailscale Funnel — public URLs, no client install

**The blocker we actually hit:** the on-site WiFi (`TADAAM_DR7MGHR`) has **client (AP) isolation
on** — ICMP ping passes between devices but *all TCP is blocked at the router*, so the Pi's LAN
IP is useless for reaching ledctl/WLED from another device on the same WiFi (e.g. the bar
Chromebook). This also silently kills SSH-over-LAN. Ping works, nothing else does = client
isolation. Can't be fixed from the Pi; only the router admin could disable it, which we don't
have access to.

**The fix that sidesteps it entirely: Tailscale Funnel.** Funnel exposes a local port at a
*public* HTTPS URL served from Tailscale's edge. Client devices reach it as **WAN traffic**
(out to the internet and back), which client isolation doesn't touch — and **nothing needs to be
installed on the client** (works on a locked-down Chromebook). The public hostname is stable, so
it's also immune to the Pi's LAN IP wandering.

Currently live (all three routes funnelled; set once, persists across reboot):

| Service | Public URL |
| --- | --- |
| ledctl operator UI | `https://xanderpi.tail182af2.ts.net:8443/?password=kaailed` |
| WLED | `https://xanderpi.tail182af2.ts.net/` |
| audio FFT | `https://xanderpi.tail182af2.ts.net:10000/` |

Funnel is only allowed on ports **443 / 8443 / 10000**, which is exactly why ledctl/WLED/audio
were mapped to those three via `tailscale serve`. Enable/disable per port (needs the invoking
user to be a Tailscale operator — `sudo tailscale set --operator=xander`, done once):

```bash
tailscale funnel --bg --https=8443 http://localhost:8000   # ledctl public
tailscale funnel --https=8443 off                          # back to tailnet-only
tailscale funnel status                                     # what's public vs tailnet-only
```

**Caveats:**
- **Only ledctl is password-gated** (`kaailed`). WLED and the audio UI have **no auth** — funnelling
  them means anyone with the (obscure but public) URL can drive them. Fine for a trusted crew;
  revert to tailnet-only if that's not acceptable on-site.
- Funnel depends on the venue's internet uplink. If it drops, the URLs die but the LED render
  loop keeps running locally (the LLM effect-authoring also needs internet regardless).
- The Pi knows both `Xander's Pixel` (hotspot) and `TADAAM_DR7MGHR`, both auto-connect. It
  prefers whichever is present; when the hotspot leaves it falls back to tadaam and Funnel keeps
  working over tadaam's uplink.

This supersedes the QR-code / fixed-IP plans below **for any network with client isolation**.
The QR + fixed-IP route (§2–3) is still the better answer for *guest phones* on a network where
you *can* disable isolation (guests won't bookmark a long funnel URL, but they'll scan a QR).

---

## 1. Current state of phone access

The Pi is already set up to serve the operator UI to any device on the same network. The pieces that exist today:

- **Auth middleware** (`src/ledctl/api/auth.py`): shared-password gate. Cookie `ledctl_auth`, 30-day lifetime. Three ways in:
  - `POST /login` (form on the login page)
  - `GET /login?password=kaailed`
  - **Any URL with `?password=kaailed` appended** — sets the cookie and forwards. This is the "scan once and you're in" entry point.
- **Network binding**: `server.host: 0.0.0.0` in `config/config.pi.yaml` → already listening on every interface (ethernet to Gledopto + WiFi).
- **Password**: `auth.password: kaailed` in `config/config.pi.yaml`.

So *server-side* access is solved. The friction is **discoverability + typing** on a phone.

### Why Chrome wouldn't connect on the hotspot

Two likely culprits when guests typed `http://<pi-ip>:8000`:

- **Pixel hotspot client isolation.** Pixel hotspots sandbox clients from each other under some security settings, so guest phone ↔ Pi traffic gets dropped at the AP. CLAUDE.md already documents the workaround (WPA2-Personal + 2.4 GHz / Extend compatibility ON). Will not be an issue on a real WiFi router.
- **`xanderpi.local` mDNS.** iOS Safari and Firefox resolve it; Android Chrome usually does not. If they typed the hostname rather than the IP, that's the failure mode.

---

## 2. Easiest-typing fix: QR codes

Even with auth working and the network sorted, guests still have to type `http://<pi-ip>:8000/?password=kaailed`. The fix is a QR code containing exactly that URL — scan, phone is logged in with the cookie set, no typing.

Two complementary surfaces (small, ~80 line diff):

- **CLI helper** — `ledctl share` that prints current per-interface IPs + an ANSI-block QR code straight into the SSH terminal. Useful when standing next to a guest with an SSH session open from the Mac.
- **`/share` page in the operator UI** — auth-gated like everything else. You (already authed) open it on your Mac/phone, hold the screen up, guest scans. Server renders an SVG QR from `request.url.netloc` + the configured password.

Tiny dep: `segno` (pure-Python, renders SVG and ANSI without Pillow). Slightly lighter than `qrcode`.

**Status: not implemented yet** — decision pending after the network swap.

---

## 3. Fixed IP on the new on-site WiFi

Two clean ways to make the Pi land on the same IP every boot.

### Option A — DHCP reservation on the router *(recommended if you have admin access)*

Pi keeps doing normal DHCP; the router always hands it the same lease.

1. Get the Pi's WiFi MAC once:
   ```bash
   ip link show wlan0 | awk '/ether/ {print $2}'
   ```
2. In the router admin panel, add a static lease: `<MAC> → <chosen IP>`.

Survives Pi reboots, OS reinstalls, even moving the Pi later (just re-do the reservation on the next network). Nothing to maintain on the Pi.

### Option B — Static IP configured on the Pi *(use if you can't touch the router)*

Detect which network stack the Pi is on (modern Pi OS Bookworm uses NetworkManager; Bullseye and older use dhcpcd):

```bash
systemctl is-active NetworkManager dhcpcd
```

**If NetworkManager is active:**

```bash
# 1. add the SSID if not already known
nmcli dev wifi connect "VenueWiFi" password "venuepassword"

# 2. pin a static IP on that connection profile
sudo nmcli con mod "VenueWiFi" \
  ipv4.method manual \
  ipv4.addresses 192.168.1.50/24 \
  ipv4.gateway 192.168.1.1 \
  ipv4.dns "192.168.1.1 1.1.1.1"

sudo nmcli con up "VenueWiFi"
```

Profile is persisted in `/etc/NetworkManager/system-connections/` and reapplied on every boot.

**If dhcpcd is active**, append to `/etc/dhcpcd.conf`:

```
interface wlan0
static ip_address=192.168.1.50/24
static routers=192.168.1.1
static domain_name_servers=192.168.1.1
```

Then `sudo systemctl restart dhcpcd`.

### Two things to nail down before picking the IP

1. **Venue network subnet + gateway.** Connect via DHCP first, then:
   ```bash
   ip route | grep default       # gateway
   ip -4 addr show wlan0         # subnet mask
   ```
2. **An IP outside the router's DHCP pool.** Most consumer routers hand out `.100–.200`; parking the Pi on `.50` (or whatever is below the pool) avoids collisions. Check "DHCP range" in router admin.

---

## 4. Suggested rollout order

1. **First**: switch to the venue WiFi and pin a fixed IP via Option A (router reservation) or B (Pi-side static) — pick based on whether you have router admin access.
2. **Then**: ship the `/share` QR page + `ledctl share` CLI helper. Once the IP is fixed, the QR encodes a URL that stays valid for the whole festival.
3. **Optional**: also keep Tailscale around as the remote-admin path, exactly as it works today.

---

## 5. Things considered and rejected

- **Static IP on the Pixel hotspot** — Pixel hotspots don't expose DHCP reservations. Moot once we leave the hotspot.
- **Captive-portal / DNS rewriting on the Pi** — way too much plumbing for "type one URL."
- **Hardening mDNS for Android** — broken on Android Chrome by design; can't fix from our side.
- **Shorter password / 4-digit PIN** — `kaailed` is already short; typing pain is dominated by the IP + port, not the password. QR sidesteps both.

---

## 6. Remote resilience (unattended, headless)

The Pi ships to a venue where nobody can touch it. Two failure modes to survive
without a human: (a) it never gets onto TADAAM at boot, and (b) it silently loses
the internet mid-show (so the operator UI over Funnel and the LLM effect-authoring
both die and there's no way in to fix it).

### a) Always reconnect to TADAAM at boot + retry — NetworkManager

`TADAAM_DR7MGHR` autoconnects with a higher priority (100) than the phone hotspot
(10), so it wins whenever it's present, and retries **forever** instead of giving
up after NM's default 4 tries:

```bash
sudo nmcli con mod TADAAM_DR7MGHR \
  connection.autoconnect yes \
  connection.autoconnect-priority 100 \
  connection.autoconnect-retries 0        # 0 = infinite; -1 = NM default (4)
```

`ipv4` is already pinned manual to `192.168.0.200/24` so the URL/IP never wanders.

### b) Reboot after 10 min offline — `net-watchdog` service

`deploy/net-watchdog.sh` + `deploy/net-watchdog.service`: a root systemd service
that probes public internet (anycast resolvers, not the LAN gateway — TADAAM has
client isolation) every 30 s. While offline it re-nudges NM onto TADAAM; if the
internet stays down for **10 min straight** it `systemctl reboot`s the Pi, which
re-runs the whole bring-up. The countdown uses monotonic uptime, so an NTP step
when the network returns can't skew it; a fresh boot resets the counter, so there's
no reboot loop while genuinely offline beyond the one 10-min cycle.

Install:

```bash
sudo cp deploy/net-watchdog.sh /usr/local/bin/net-watchdog.sh
sudo chmod +x /usr/local/bin/net-watchdog.sh
sudo cp deploy/net-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now net-watchdog.service
journalctl -u net-watchdog -f            # watch the countdown / restore logs
```

Tunables live in the unit as `Environment=` overrides — `CHECK_INTERVAL`,
`FAIL_LIMIT` (default 600 s), `GRACE` (ignore the first 120 s after boot),
`WIFI_CONN`. Bench work with no uplink: `sudo systemctl stop net-watchdog` so it
doesn't reboot the Pi out from under you.
