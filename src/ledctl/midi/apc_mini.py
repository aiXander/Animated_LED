"""The Akai APC mini (mk1): its controls, where they sit, and how its lights work.

Hardware ids are `note:<n>` (a pad or button) and `cc:<n>` (a fader); the MIDI
channel is ignored. `at` is [column, row] on a 9 x 10 drawing, row 0 at the top.
A control with `reserved` text belongs to WZRD in every mode (it never forwards
it), so ledctl cannot bind it; the text says what it does there.
"""

from __future__ import annotations

from typing import Any

ID = "apc-mini"
NAME = "Akai APC mini"
COLS, ROWS = 9, 10

# Light values: a note-on [0x90, note, value] sets a control's light.
PAD_OFF, PAD_GREEN, PAD_GREEN_BLINK, PAD_RED, PAD_YELLOW = 0, 1, 2, 3, 5
SINGLE_OFF, SINGLE_ON = 0, 1

_RIGHT = ["Clip stop", "Solo", "Rec arm", "Mute", "Select", "Button 6", "Button 7",
          "Stop all clips"]
_BOTTOM = ["▲", "▼", "◀", "▶", "Volume", "Pan", "Send", "Device"]
_MODES = {68: "Layers", 69: "Masters", 70: "Layer FX", 71: "LEDs (this mode)"}


def controls() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in range(8):
        for col in range(8):
            out.append({"hw": f"note:{(7 - row) * 8 + col}", "label": f"Pad {row + 1}·{col + 1}",
                        "kind": "button", "led": "pad", "at": [col, row]})
        out.append({"hw": f"note:{82 + row}", "label": _RIGHT[row],
                    "kind": "button", "led": "single", "at": [8, row]})
    for col in range(8):
        note = 64 + col
        c = {"hw": f"note:{note}", "label": _BOTTOM[col],
             "kind": "button", "led": "single", "at": [col, 8]}
        if note in _MODES:
            c["reserved"] = f"WZRD: switch to the {_MODES[note]} mode."
        out.append(c)
    out.append({"hw": "note:98", "label": "Shift", "kind": "button", "led": None, "at": [8, 8],
                "reserved": "WZRD: tap for the next mode, hold for the shift layer."})
    for i in range(9):
        c = {"hw": f"cc:{48 + i}", "label": f"Fader {i + 1}",
             "kind": "fader", "led": None, "at": [i, 9]}
        if i == 8:
            c["reserved"] = "WZRD: master brightness of the wall, in every mode."
        out.append(c)
    return out


def default_bindings(effects: list[str]) -> dict[str, str]:
    """The preset: one library effect per pad from the top-left, row by row
    (`effects` in the order to lay them out), then the faders and buttons."""
    b: dict[str, str] = {}
    for i, name in enumerate(effects[:64]):
        row, col = divmod(i, 8)
        b[f"note:{(7 - row) * 8 + col}"] = f"effect:{name}"
    faders = ["master.brightness", "master.speed", "master.audio_reactivity",
              "master.sparkle", "master.glitch", "master.strobe", "master.trails",
              "wzrd.colour"]
    for i, target in enumerate(faders):
        b[f"cc:{48 + i}"] = target
    b.update({"note:82": "blackout", "note:83": "playlist", "note:84": "flash",
              "note:85": "fx.clear", "note:66": "effect.prev", "note:67": "effect.next"})
    return b
