"""SenseIQ sleep-tracking payloads (DPS 3 and 4).

SenseIQ is not a separate Philips service: it is a block of Tuya data points on
the low DPS range (ids 1-21), arriving through the same cloud poll and LAN push
as the temperature. The two that carry the actual signal:

- DPS 3 `sleepiq_status`, a plain JSON string with the instantaneous reading,
  e.g. `{"r":"b","br":29}` (live SCD953, 2026-08-09). `br` is the breathing
  rate in breaths per minute — **confirmed** by comparing against the Philips
  app's own reading (27, 28, 30, 33 observed, all matching). `r` is NOT the
  sleep state: it read `"b"` across an hour of samples while the sleep state
  changed underneath it. Its meaning is unknown (best guess "baby detected"),
  so it is relayed verbatim, never interpreted.
- DPS 4 `sleep_session_data`, the session in progress. The camera double-wraps
  it: base64 of the ASCII hex of the JSON, e.g.
  `{"st":1786297106,"sd":1864,"css":"d","cssd":1562,"ssd":[{"l":302}]}`.
  `st` is the session start (epoch seconds, stable across polls), `sd` the
  running session duration in seconds (verified to grow with the wall clock),
  `css` the current sleep state letter code with `cssd` seconds spent in it,
  and `ssd` the completed earlier segments — the arithmetic
  `sd == sum(ssd durations) + cssd` held on every observed sample.

The sleep-state vocabulary, as far as paired observations against the app have
established it (2026-08-09):

- `d` = deep sleep — **confirmed**: the app showed "deep sleep" at the moment
  `css` read `'d'`.
- `l` = light sleep — **inferred**: `ssd` alternates `l` and `d` exactly as
  sleep cycles do, e.g. `[{l:302},{d:2258},{l:710},{d:286},{l:354}]` with
  `cssd=300` summing to `sd=4210` exactly.

Any other code is unknown and must surface as None, never as the raw letter:
an unrecognised code must not look like a real state, because a sensor that
guesses wrong about whether a baby sleeps is worse than one that shows
unknown.

No Home Assistant imports here, so the parsing is unit-tested on its own.
"""
from __future__ import annotations

import base64
import binascii
import json

SLEEP_STATE_DEEP = "deep"
SLEEP_STATE_LIGHT = "light"

# The decoded sleep-state vocabulary. `d` is confirmed against the app;
# `l` is inferred from the alternation of the `ssd` timeline (see module
# docstring). Everything else is unknown and maps to None on purpose.
SLEEP_STATE_CODES = {
    "d": SLEEP_STATE_DEEP,
    "l": SLEEP_STATE_LIGHT,
}

# The closed set of translated states, for a Home Assistant ENUM sensor's
# `options`: HA rejects any state outside this list, which is exactly the
# safety property wanted here — an unmapped code becomes unknown, not a state.
SLEEP_STATES = [SLEEP_STATE_DEEP, SLEEP_STATE_LIGHT]


def decode_senseiq_payload(raw: object) -> dict | None:
    """Decode a SenseIQ DPS value into a dict, or None for anything unreadable.

    Accepts a dict as-is, plain JSON text, base64-wrapped JSON, and the
    base64-of-hex-of-JSON double wrapping observed on DPS 4. Anything that does
    not resolve to a JSON object is None: an unreadable payload must degrade to
    an unknown sensor, never to a wrong answer.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None

    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        if not text.startswith("{"):
            try:
                text = bytes.fromhex(text.strip()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _number(payload: dict | None, key: str) -> float | None:
    """A numeric field of a payload, or None. Booleans are not numbers here."""
    if not payload:
        return None
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def session_start(payload: dict | None) -> float | None:
    """Session start as seconds since epoch (`st`), or None.

    Only observed in seconds so far, but a millisecond stamp is folded down the
    same way events.py does for alarm records, since firmwares differ there.
    """
    stamp = _number(payload, "st")
    if stamp is None:
        return None
    if stamp > 1e11:
        stamp /= 1000.0
    return stamp if stamp > 0 else None


def session_duration(payload: dict | None) -> int | None:
    """Running session duration in seconds (`sd`), or None."""
    value = _number(payload, "sd")
    if value is None or value < 0:
        return None
    return int(value)


def sleep_state(payload: dict | None) -> str | None:
    """The current sleep state of a DPS 4 payload (`css`), translated.

    `d` maps to "deep" (confirmed against the app), `l` to "light" (inferred
    from the segment timeline). Anything else — missing, non-string, or an
    unrecognised code — is None, never the raw letter: the caller exposes this
    as an ENUM state and an unmapped code must read unknown, not pass as real.
    """
    if not payload:
        return None
    code = payload.get("css")
    if not isinstance(code, str):
        return None
    return SLEEP_STATE_CODES.get(code.strip())


def breathing_rate(payload: dict | None) -> float | None:
    """Breathing rate in breaths per minute (`br` of DPS 3), or None.

    Confirmed against the app's own breathing-rate display (27, 28, 30 and 33
    all matched). Negative values are rejected as garbage; zero is relayed
    verbatim, since editing a measurement is not this layer's call.
    """
    value = _number(payload, "br")
    if value is None or value < 0:
        return None
    return value


def status_code(payload: dict | None) -> str | None:
    """The raw `r` letter code of a DPS 3 status payload, or None.

    NOT the sleep state — it read "b" for an hour while the sleep state (DPS 4
    `css`) changed underneath it. Meaning unknown; relayed verbatim as
    evidence, never translated.
    """
    if not payload:
        return None
    value = payload.get("r")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def status_attributes(payload: dict | None) -> dict:
    """Everything a DPS 3 payload carries besides the state code, verbatim."""
    if not payload:
        return {}
    return {key: value for key, value in payload.items() if key != "r"}


def session_attributes(payload: dict | None) -> dict:
    """The state timeline of a DPS 4 payload, verbatim, for attributes.

    `css`/`cssd` are the current state code and how long it has held; `ssd` the
    completed earlier segments. The raw `css` letter rides along even though
    `sleep_state` translates it, so an unmapped code stays visible as evidence
    instead of vanishing into an unknown state.
    """
    if not payload:
        return {}
    attrs = {}
    if "css" in payload:
        attrs["current_state"] = payload["css"]
    if "cssd" in payload:
        attrs["current_state_duration"] = payload["cssd"]
    if "ssd" in payload:
        attrs["state_segments"] = payload["ssd"]
    return attrs
