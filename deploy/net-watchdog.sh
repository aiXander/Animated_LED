#!/usr/bin/env bash
# net-watchdog.sh — remote-resilience for the headless festival Pi.
#
# Two jobs, both aimed at "the Pi is unreachable and nobody is on-site to touch
# it":
#   1. While the internet is unreachable, keep nudging NetworkManager to
#      re-attach to the venue WiFi (TADAAM). NM's own autoconnect does most of
#      this; this is the belt-and-braces re-attempt for the case where NM has
#      given up or is stuck on a dead association.
#   2. If the internet stays unreachable for FAIL_LIMIT seconds straight,
#      reboot the Pi. A reboot re-runs the whole bring-up (NM autoconnect,
#      gledopto-reboot, ledctl, tailscale) and is the last-resort recovery for
#      a wedged network stack we can't SSH into to fix by hand.
#
# Reachability = can we reach the public internet, not just the LAN. The venue
# WiFi (TADAAM) has client isolation, and the whole point of being online is the
# LLM effect-authoring + Tailscale Funnel, both of which need real WAN — so we
# probe public anycast resolvers rather than the gateway.
#
# Tunables via the service's Environment= (all seconds):
#   CHECK_INTERVAL  probe cadence                         (default 30)
#   FAIL_LIMIT      offline this long -> reboot           (default 600 = 10 min)
#   GRACE           ignore this long after boot           (default 120)
#   WIFI_CONN       NM connection name to force up         (default TADAAM_DR7MGHR)
#
# Install: see deploy/net-watchdog.service.

set -uo pipefail

CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
FAIL_LIMIT="${FAIL_LIMIT:-600}"
GRACE="${GRACE:-120}"
WIFI_CONN="${WIFI_CONN:-TADAAM_DR7MGHR}"
PROBE_HOSTS=(1.1.1.1 8.8.8.8 9.9.9.9)

log() { echo "net-watchdog: $*"; }

# Monotonic seconds since boot — immune to wall-clock jumps (NTP step after the
# network comes back would otherwise skew a wall-clock countdown).
mono() { cut -d. -f1 /proc/uptime; }

online() {
  local h
  for h in "${PROBE_HOSTS[@]}"; do
    ping -n -c1 -W3 "$h" >/dev/null 2>&1 && return 0
  done
  return 1
}

# Let NM finish its first autoconnect pass before we start judging it.
log "starting (interval=${CHECK_INTERVAL}s reboot-after=${FAIL_LIMIT}s grace=${GRACE}s)"
sleep "$GRACE"

down_since=0
while true; do
  if online; then
    if (( down_since != 0 )); then
      log "internet restored after $(( $(mono) - down_since ))s offline"
      down_since=0
    fi
  else
    now=$(mono)
    (( down_since == 0 )) && { down_since=$now; log "internet unreachable — countdown to reboot started"; }

    # Nudge NM back onto the venue WiFi while we wait for the countdown.
    nmcli con up "$WIFI_CONN" >/dev/null 2>&1 \
      || nmcli device connect wlan0 >/dev/null 2>&1 || true

    down=$(( now - down_since ))
    log "still offline for ${down}s / ${FAIL_LIMIT}s"
    if (( down >= FAIL_LIMIT )); then
      log "offline ${down}s >= ${FAIL_LIMIT}s — rebooting now"
      systemctl reboot
      sleep 120   # hold here so we don't loop/re-trigger while the reboot lands
    fi
  fi
  sleep "$CHECK_INTERVAL"
done
