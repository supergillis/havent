"""SenseIQ sleep-tracking payloads (DPS 3 and 4).

SenseIQ is not a separate Philips service: it is a block of Tuya data points on
the low DPS range (ids 1-21), arriving through the same cloud poll and LAN push
as the temperature. The two that carry the actual signal:

- DPS 3 `sleepiq_status`, a plain JSON string with the instantaneous reading,
  e.g. `{"r":"b","br":29}` (live SCD953, 2026-08-09). The `r` letter code is a
  device state whose vocabulary is not decoded yet; `br` is a number that moved
  between 29 and 33 across observations.
- DPS 4 `sleep_session_data`, the session in progress. The camera double-wraps
  it: base64 of the ASCII hex of the JSON, e.g.
  `{"st":1786297106,"sd":1864,"css":"d","cssd":1562,"ssd":[{"l":302}]}`.
  `st` is the session start (epoch seconds, stable across polls), `sd` the
  running session duration in seconds (verified to grow with the wall clock),
  `css` the current state letter code with `cssd` seconds spent in it, and
  `ssd` the completed earlier segments — the arithmetic
  `sd == sum(ssd durations) + cssd` held on every observed sample.

The letter codes (`r`, `css`, and the keys inside `ssd` entries) are passed
through verbatim and never translated to "asleep" or "awake": their vocabulary
would have to be established by correlating against a known baby state, and a
sensor that guesses wrong about whether a baby sleeps is worse than one that
shows an opaque code.

No Home Assistant imports here, so the parsing is unit-tested on its own.
"""
from __future__ import annotations

import base64
import binascii
import json


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


def status_code(payload: dict | None) -> str | None:
    """The raw state letter code of a DPS 3 status payload (`r`), or None."""
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
    """The undecoded remainder of a DPS 4 payload, verbatim, for attributes.

    `css`/`cssd` are the current state code and how long it has held; `ssd` the
    completed earlier segments. Kept as raw device values so the recorder
    collects the evidence needed to decode the vocabulary later.
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
