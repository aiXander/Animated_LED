"""Is the machine itself holding the render loop back? (Raspberry Pi only.)

A Pi in a sealed enclosure throttles its CPU when it gets hot or when the
supply sags, and the default CPU governor clocks down between frames. Both
show up as "the LEDs lag" with nothing wrong in the software, so the operator
UI reports them next to the frame timings.

Everything is read from sysfs, best effort: on a machine without these files
(the Mac) every field is None.
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any

_TEMP = Path("/sys/class/thermal/thermal_zone0/temp")
_FREQ = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
_GOVERNOR = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
# Raspberry Pi firmware flags: bit 0 under-voltage, 1 frequency capped,
# 2 throttled, 3 soft temperature limit; bits 16-19 = "has happened since boot".
_THROTTLED = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")

_CACHE_S = 5.0
_cache: tuple[float, dict[str, Any]] | None = None


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def host_health() -> dict[str, Any]:
    """Temperature, clock speed, governor and throttling flags. Cached 5 s."""
    global _cache
    now = monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_S:
        return _cache[1]
    temp = _read(_TEMP)
    freq = _read(_FREQ)
    flags_raw = _read(_THROTTLED)
    try:
        flags = int(flags_raw, 16) if flags_raw is not None else None
    except ValueError:
        flags = None
    out: dict[str, Any] = {
        "temp_c": round(int(temp) / 1000.0, 1) if temp and temp.isdigit() else None,
        "cpu_mhz": round(int(freq) / 1000.0) if freq and freq.isdigit() else None,
        "governor": _read(_GOVERNOR),
        # Slowed down right now (frequency capped, throttled or soft temp limit).
        "throttled": bool(flags & 0xE) if flags is not None else None,
        "undervoltage": bool(flags & 0x1) if flags is not None else None,
        # Either of the above has happened since boot.
        "throttled_since_boot": bool(flags & 0xF0000) if flags is not None else None,
    }
    _cache = (now, out)
    return out


__all__ = ["host_health"]
